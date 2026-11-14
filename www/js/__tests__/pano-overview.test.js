// Offline unit tests for pano-overview.js — the city map's viewport-true dot
// tier (issue #77): the slippy-map cell math, the spatial index, the exact
// in-view count and id set, and the pure view planner. Run with
// `node --test "js/__tests__/**/*.test.js"` — no network, no Leaflet, no DOM.
//
// The planner is where the fast, blocking suite protects this feature: the
// e2e job is advisory, so the tier rule, its hysteresis, the Panoramas and
// flat-only switches and the date semantics are all pinned here.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  TIER_LEAVE_FACTOR,
  TIER_ANNOUNCEMENTS,
  mercatorCell,
  mortonEncode,
  mortonDecode,
  createPanoColumns,
  populationFilter,
  indexLayout,
  buildPanoIndex,
  indexCellOf,
  indexByteLength,
  countPerCell,
  countInBounds,
  idsInBounds,
  isViewDependent,
  planMapView,
  drawnNoticeText,
} = require("../pano-overview.js");

// --- helpers ------------------------------------------------------------------

/** Seeded PRNG (mulberry32), so every random case is reproducible. */
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Columns of `n` seeded points uniformly inside a box, with years 2008..2025
 * and roughly a third of them non-official.
 */
function seededColumns(n, { south, west, north, east }, seed) {
  const r = rng(seed);
  const cols = createPanoColumns(4);
  for (let i = 0; i < n; i++) {
    cols.push(
      south + (north - south) * r(),
      west + (east - west) * r(),
      2008 + Math.floor(r() * 18),
      r() > 0.33
    );
  }
  return cols.finish();
}

function bruteIds(cols, bounds, accept) {
  const out = [];
  for (let id = 0; id < cols.length; id++) {
    const la = cols.lat[id];
    const lo = cols.lon[id];
    if (accept(id) && la >= bounds.south && la <= bounds.north && lo >= bounds.west && lo <= bounds.east) {
      out.push(id);
    }
  }
  return out;
}

/** A random sub-box of `box`, sometimes reaching past it. */
function randomView(r, { south, west, north, east }) {
  const dLat = north - south;
  const dLon = east - west;
  const a = south - 0.1 * dLat + 1.2 * dLat * r();
  const b = south - 0.1 * dLat + 1.2 * dLat * r();
  const c = west - 0.1 * dLon + 1.2 * dLon * r();
  const d = west - 0.1 * dLon + 1.2 * dLon * r();
  return { south: Math.min(a, b), north: Math.max(a, b), west: Math.min(c, d), east: Math.max(c, d) };
}

// A city-sized box straddling a zoom-8 tile CORNER (x 42|43, y 90|91), so a
// coarse cell boundary runs through it in both directions.
const Z8_CORNER_LON = (43 / 256) * 360 - 180;
const Z8_CORNER_LAT = (Math.atan(Math.sinh(Math.PI * (1 - (2 * 91) / 256))) * 180) / Math.PI;
const STRADDLING_CITY = {
  south: Z8_CORNER_LAT - 0.15,
  north: Z8_CORNER_LAT + 0.12,
  west: Z8_CORNER_LON - 0.2,
  east: Z8_CORNER_LON + 0.17,
};

const FILTERS = [
  { officialOnly: false, year: null },
  { officialOnly: true, year: null },
  { officialOnly: false, year: 2019 },
  { officialOnly: true, year: 2012 },
];

// --- cell math ------------------------------------------------------------------

test("mercatorCell matches the committed golden (the Python tile math) at every level", () => {
  const golden = JSON.parse(fs.readFileSync(path.join(__dirname, "pano-cell-golden.json"), "utf8"));
  assert.ok(golden.entries.length >= 50);
  for (const e of golden.entries) {
    for (const [level, [x, y]] of Object.entries(e.cells)) {
      assert.deepEqual(mercatorCell(e.lat, e.lon, Number(level)), { x, y }, `${e.name} at level ${level}`);
    }
  }
});

test("mercatorCell keeps x and y inside 0..2^z-1 at the poles and the antimeridian", () => {
  for (let z = 0; z <= 22; z++) {
    const n = 2 ** z;
    for (const lat of [90, 89.999, 89, 85.06, -85.06, -89, -90]) {
      for (const lon of [-180, 0, 179.9999999, 180]) {
        const { x, y } = mercatorCell(lat, lon, z);
        assert.ok(Number.isInteger(x) && x >= 0 && x < n, `x ${x} at z${z}, (${lat}, ${lon})`);
        assert.ok(Number.isInteger(y) && y >= 0 && y < n, `y ${y} at z${z}, (${lat}, ${lon})`);
      }
    }
    // North is row 0 and south the last row, as Leaflet numbers tiles.
    assert.equal(mercatorCell(89, 10, z).y, 0);
    assert.equal(mercatorCell(-89, 10, z).y, n - 1);
  }
});

test("Morton codes round-trip and keep x and y apart", () => {
  const r = rng(3);
  for (let i = 0; i < 2000; i++) {
    const x = Math.floor(r() * 65536);
    const y = Math.floor(r() * 65536);
    const code = mortonEncode(x, y);
    assert.ok(code >= 0 && code < 2 ** 32);
    assert.deepEqual(mortonDecode(code), { x, y });
  }
  assert.equal(mortonEncode(1, 0), 1);
  assert.equal(mortonEncode(0, 1), 2);
});

// --- the index ------------------------------------------------------------------

test("indexLayout keeps every packed sort key exact in a Float64", () => {
  assert.deepEqual(indexLayout(60000), { level: 16, idBits: 16, packed: true });
  assert.deepEqual(indexLayout(2 ** 20), { level: 16, idBits: 20, packed: true });
  assert.deepEqual(indexLayout(2 ** 20 + 1), { level: 15, idBits: 21, packed: true });
  assert.deepEqual(indexLayout(2 ** 22 + 1), { level: 14, idBits: 23, packed: true });
  assert.deepEqual(indexLayout(2 ** 24), { level: 14, idBits: 24, packed: true });
  assert.deepEqual(indexLayout(2 ** 24 + 1), { level: 14, idBits: 25, packed: false });
  for (let n = 2; n < 2 ** 26; n = Math.ceil(n * 1.7)) {
    const { level, idBits, packed } = indexLayout(n);
    assert.ok(2 ** idBits >= n, `ids fit at n=${n}`);
    if (packed) assert.ok(2 * level + idBits <= 52, `key exact at n=${n}`);
  }
});

test("every point's index cell IS the slippy-map cell, at every level down to 0", () => {
  // The review's C1: an origin-relative code makes coarse cells that straddle
  // real tiles. Asserting actual coordinates (not parent/child sums) is what
  // makes an x/y transposition fail here.
  for (const [box, seed] of [[STRADDLING_CITY, 11], [{ south: 47.4, north: 47.8, west: -122.5, east: -122.1 }, 12]]) {
    const cols = seededColumns(3000, box, seed);
    const index = buildPanoIndex(cols);
    assert.equal(index.level, 16);
    for (let id = 0; id < cols.length; id++) {
      for (let level = index.level; level >= 0; level--) {
        assert.deepEqual(indexCellOf(index, id, level), mercatorCell(cols.lat[id], cols.lon[id], level));
      }
    }
  }
  // ...and the straddling city really does straddle the zoom-8 corner.
  const cols = seededColumns(3000, STRADDLING_CITY, 11);
  const index = buildPanoIndex(cols);
  const z8 = new Set();
  for (let id = 0; id < cols.length; id++) {
    const { x, y } = indexCellOf(index, id, 8);
    z8.add(`${x},${y}`);
  }
  assert.deepEqual([...z8].sort(), ["42,90", "42,91", "43,90", "43,91"]);
});

test("the index orders points by cell then id, and the comparator fallback agrees", () => {
  const cols = seededColumns(5000, STRADDLING_CITY, 21);
  const packed = buildPanoIndex(cols);
  const fallback = buildPanoIndex(cols, { forceComparatorSort: true });
  assert.deepEqual(Array.from(fallback.order), Array.from(packed.order));
  for (let i = 1; i < packed.order.length; i++) {
    const a = packed.order[i - 1];
    const b = packed.order[i];
    assert.ok(packed.morton[a] < packed.morton[b] || (packed.morton[a] === packed.morton[b] && a < b));
  }
  // Each cell's range holds exactly the points of that cell.
  let covered = 0;
  for (let c = 0; c < packed.cellCount; c++) {
    for (let i = packed.cellStart[c]; i < packed.cellEnd[c]; i++) {
      assert.equal(packed.morton[packed.order[i]], packed.cellMorton[c]);
      covered++;
    }
  }
  assert.equal(covered, cols.length);
  assert.ok(indexByteLength(packed) >= 8 * cols.length);
});

test("countInBounds equals a brute-force count on random views, under every filter", () => {
  const cols = seededColumns(8000, STRADDLING_CITY, 31);
  const index = buildPanoIndex(cols);
  const r = rng(32);
  for (const filter of FILTERS) {
    const accept = populationFilter(cols, filter);
    const counts = countPerCell(index, accept);
    for (let k = 0; k < 150; k++) {
      const view = randomView(r, STRADDLING_CITY);
      assert.equal(countInBounds(index, cols, counts, view, accept), bruteIds(cols, view, accept).length,
        `${JSON.stringify(filter)} ${JSON.stringify(view)}`);
    }
    // The whole extent counts every admitted point.
    const all = { south: -90, north: 90, west: -180, east: 180 };
    assert.equal(countInBounds(index, cols, counts, all, accept), bruteIds(cols, all, accept).length);
  }
});

test("idsInBounds is exactly the points in view, edges included", () => {
  const cols = seededColumns(6000, STRADDLING_CITY, 41);
  const index = buildPanoIndex(cols);
  const r = rng(42);
  const accept = populationFilter(cols, { officialOnly: false, year: null });
  for (let k = 0; k < 150; k++) {
    const view = randomView(r, STRADDLING_CITY);
    assert.deepEqual(idsInBounds(index, cols, view, accept).sort((a, b) => a - b), bruteIds(cols, view, accept));
  }
  // A view whose every edge passes exactly through a pano: Leaflet's bounds
  // are inclusive, so those four panos are in view and must be drawn.
  const ids = [7, 99, 1234, 4321];
  const view = {
    south: Math.min(...ids.map((i) => cols.lat[i])),
    north: Math.max(...ids.map((i) => cols.lat[i])),
    west: Math.min(...ids.map((i) => cols.lon[i])),
    east: Math.max(...ids.map((i) => cols.lon[i])),
  };
  const got = new Set(idsInBounds(index, cols, view, accept));
  for (const i of ids) assert.ok(got.has(i), `edge pano ${i} missed`);
  assert.deepEqual([...got].sort((a, b) => a - b), bruteIds(cols, view, accept));
});

test("a pano Mercator cannot place is still counted where it really is", () => {
  // Latitude 88 lands in row 0, whose box ends at ~85.05; longitude 180 lands
  // in the last column. Such a cell's box does not bound its points, so it
  // must be walked point by point, never summed or skipped whole.
  const cols = createPanoColumns();
  cols.push(88, 10, 2020, true);
  cols.push(85.04, 10.001, 2020, true);
  cols.push(-88, 10, 2020, true);
  cols.push(10, 180, 2020, true);
  cols.push(10.0001, 179.99999, 2020, true);
  const c = cols.finish();
  const index = buildPanoIndex(c);
  const accept = populationFilter(c, { officialOnly: false, year: null });
  const counts = countPerCell(index, accept);
  for (const view of [
    { south: 80, north: 86, west: 9, east: 11 },
    { south: 87, north: 89, west: 9, east: 11 },
    { south: -89, north: -87, west: 9, east: 11 },
    { south: 9, north: 11, west: 179, east: 179.999999 },
    { south: 9, north: 11, west: 179, east: 180 },
  ]) {
    const want = bruteIds(c, view, accept);
    assert.equal(countInBounds(index, c, counts, view, accept), want.length, JSON.stringify(view));
    assert.deepEqual(idsInBounds(index, c, view, accept).sort((a, b) => a - b), want, JSON.stringify(view));
  }
});

test("populationFilter is the mode and year rule", () => {
  const cols = seededColumns(500, STRADDLING_CITY, 51);
  for (const { officialOnly, year } of FILTERS) {
    const accept = populationFilter(cols, { officialOnly, year });
    for (let id = 0; id < cols.length; id++) {
      const want = (!officialOnly || cols.official[id] === 1) && (year == null || cols.year[id] === year);
      assert.equal(accept(id), want);
    }
  }
});

// --- the view planner -------------------------------------------------------------

const BUDGET = 40000;
const LEAVE = Math.floor(BUDGET * TIER_LEAVE_FACTOR);

/** A planner state whose thunks return tagged arrays; overrides win. */
function state(overrides = {}) {
  return {
    budget: BUDGET,
    populationSize: 250000,
    renderAll: false,
    panosHidden: false,
    prevTier: "sampled",
    freezeTier: false,
    flatOn: false,
    populationItems: () => ["p1", "p2", "p3"],
    sampleItems: () => ["s1", "s2"],
    countInView: () => 1000,
    viewItems: () => ["v1", "v2", "v3", "v4"],
    dateItems: null,
    pinnedItem: null,
    ...overrides,
  };
}

const boom = (what) => () => {
  throw new Error(`${what} must not be called`);
};

test("planner: a city at or under the budget takes the pre-#77 path and never counts a view", () => {
  for (const populationSize of [0, 1, 39999, BUDGET]) {
    const plan = planMapView(state({
      populationSize,
      prevTier: null,
      countInView: boom("countInView"),
      viewItems: boom("viewItems"),
      sampleItems: boom("sampleItems"),
    }));
    assert.equal(plan.mode, "all");
    assert.equal(plan.tier, "complete");
    assert.deepEqual([...plan.desired], ["p1", "p2", "p3"]);
    assert.equal(plan.announce, null);
    assert.equal(isViewDependent({ populationSize, budget: BUDGET, renderAll: false }), false);
  }
  assert.equal(isViewDependent({ populationSize: BUDGET + 1, budget: BUDGET, renderAll: false }), true);
  // "Render all" is the same whole-population path, whatever the size.
  const all = planMapView(state({ renderAll: true, countInView: boom("countInView") }));
  assert.equal(all.mode, "all");
  assert.equal(isViewDependent({ populationSize: 10 ** 7, budget: BUDGET, renderAll: true }), false);
});

test("planner: a view within the budget draws every pano in it", () => {
  for (const count of [0, 1, BUDGET]) {
    const plan = planMapView(state({ countInView: () => count }));
    assert.equal(plan.mode, "view");
    assert.equal(plan.tier, "complete");
    assert.equal(plan.inViewCount, count);
    assert.deepEqual([...plan.desired], ["v1", "v2", "v3", "v4"]);
  }
  const over = planMapView(state({ countInView: () => BUDGET + 1 }));
  assert.equal(over.mode, "sample");
  assert.equal(over.tier, "sampled");
  assert.deepEqual([...over.desired], ["s1", "s2"]);
});

test("planner: hysteresis — enter at the budget, stay through the band, leave above it", () => {
  // Enter: from the sample, only at <= budget.
  assert.equal(planMapView(state({ prevTier: "sampled", countInView: () => BUDGET })).tier, "complete");
  assert.equal(planMapView(state({ prevTier: "sampled", countInView: () => BUDGET + 1 })).tier, "sampled");
  // Stay: once complete, anywhere in the band.
  for (const count of [BUDGET + 1, Math.floor(BUDGET * 1.1), LEAVE]) {
    const plan = planMapView(state({ prevTier: "complete", countInView: () => count }));
    assert.equal(plan.tier, "complete", `count ${count}`);
    assert.equal(plan.announce, null);
  }
  // Leave: only above 1.25 x budget.
  const left = planMapView(state({ prevTier: "complete", countInView: () => LEAVE + 1 }));
  assert.equal(left.tier, "sampled");
  assert.equal(LEAVE, 50000);
});

test("planner: announces once per actual tier change, never on the first plan or a plain pan", () => {
  assert.equal(
    planMapView(state({ prevTier: "sampled", countInView: () => 10 })).announce,
    TIER_ANNOUNCEMENTS.complete
  );
  assert.equal(
    planMapView(state({ prevTier: "complete", countInView: () => LEAVE + 1 })).announce,
    TIER_ANNOUNCEMENTS.sampled
  );
  assert.equal(planMapView(state({ prevTier: "complete", countInView: () => 10 })).announce, null);
  assert.equal(planMapView(state({ prevTier: "sampled", countInView: () => 10 ** 6 })).announce, null);
  assert.equal(planMapView(state({ prevTier: null, countInView: () => 10 })).announce, null);
  assert.match(TIER_ANNOUNCEMENTS.sampled, /zoom in/i);
});

test("planner: a popup's own auto-pan never re-tiers", () => {
  const frozen = planMapView(state({ prevTier: "complete", freezeTier: true, countInView: () => 10 ** 6 }));
  assert.equal(frozen.tier, "complete");
  assert.equal(frozen.announce, null);
  const frozenSample = planMapView(state({ prevTier: "sampled", freezeTier: true, countInView: () => 1 }));
  assert.equal(frozenSample.tier, "sampled");
  assert.deepEqual([...frozenSample.desired], ["s1", "s2"]);
  // ...and the open popup's pano is kept drawn.
  const pinned = planMapView(state({ prevTier: "complete", countInView: () => 5, pinnedItem: "popup" }));
  assert.ok(pinned.desired.has("popup"));
});

test("planner: flat-only points follow their own switch, never the Panoramas layer", () => {
  for (const flatOn of [false, true]) {
    for (const panosHidden of [false, true]) {
      const plan = planMapView(state({ flatOn, panosHidden }));
      assert.equal(plan.flatShown, flatOn, `flatOn=${flatOn} panosHidden=${panosHidden}`);
      if (panosHidden) {
        assert.equal(plan.mode, "hidden");
        assert.equal(plan.desired.size, 0);
      } else {
        // Turning flat points on or off changes nothing about the pano dots.
        const other = planMapView(state({ flatOn: !flatOn, panosHidden }));
        assert.deepEqual([...plan.desired], [...other.desired]);
        assert.equal(plan.tier, other.tier);
      }
    }
  }
});

test("planner: the Panoramas layer off hides every dot and keeps the tier, so turning it on is silent", () => {
  const hidden = planMapView(state({ panosHidden: true, prevTier: "sampled", countInView: boom("countInView") }));
  assert.equal(hidden.tier, "sampled");
  assert.equal(hidden.announce, null);
  const back = planMapView(state({ prevTier: hidden.tier, countInView: () => BUDGET + 5 }));
  assert.equal(back.announce, null);
});

test("planner: a selected date only adds its panos; it never decides the tier or narrows the set", () => {
  for (const [count, prevTier] of [[10, "sampled"], [BUDGET + 1, "sampled"], [LEAVE, "complete"]]) {
    const without = planMapView(state({ prevTier, countInView: () => count }));
    const withDate = planMapView(state({ prevTier, countInView: () => count, dateItems: () => ["d1", "v1"] }));
    assert.equal(withDate.tier, without.tier);
    assert.equal(withDate.inViewCount, without.inViewCount);
    assert.deepEqual([...withDate.desired].sort(), [...new Set([...without.desired, "d1", "v1"])].sort());
  }
  const under = planMapView(state({ populationSize: 10, dateItems: () => ["d1"] }));
  assert.deepEqual([...under.desired].sort(), ["d1", "p1", "p2", "p3"]);
});

test("planner: an open popup's pano is kept on every re-plan, and dropped only when it leaves the population", () => {
  // The planner takes no "cause": a move, a date key, the flat toggle and a
  // Render-all round trip all re-plan through it, and each must keep the pin.
  const causes = {
    move: {},
    date: { dateItems: () => ["d1"], dateKey: "Mon Apr 13 2026" },
    flatToggle: { flatOn: true },
    renderAllOn: { renderAll: true },
    renderAllOff: { renderAll: false },
  };
  for (const [prevTier, count] of [["complete", 5], ["sampled", 10 ** 6]]) {
    for (const [cause, extra] of Object.entries(causes)) {
      const plan = planMapView(state({
        prevTier, countInView: () => count, pinnedItem: "popup", inPopulation: () => true, ...extra,
      }));
      assert.ok(plan.desired.has("popup"), `${cause} in ${prevTier}`);
      assert.equal(plan.pinnedItem, "popup");
    }
  }
  // A year or imagery mode the reader chose that excludes the pano drops it.
  const excluded = planMapView(state({ countInView: () => 5, pinnedItem: "popup", inPopulation: () => false }));
  assert.equal(excluded.desired.has("popup"), false);
  assert.equal(excluded.pinnedItem, null);
  // So does turning the Panoramas layer off.
  assert.equal(planMapView(state({ panosHidden: true, pinnedItem: "popup" })).desired.size, 0);
});

test("planner: a re-plan in the same sample reuses its set and builds nothing", () => {
  const first = planMapView(state({ prevTier: "sampled", countInView: () => 10 ** 6, sampleKey: "g0|true|null" }));
  assert.equal(first.reused, false);
  // A pan that stays sampled: the same Set object, and neither list is asked for.
  const pan = planMapView(state({
    prevTier: "sampled", prevPlan: first, countInView: () => 10 ** 6 + 7, sampleKey: "g0|true|null",
    sampleItems: boom("sampleItems"), dateItems: null,
  }));
  assert.equal(pan.reused, true);
  assert.equal(pan.desired, first.desired);
  assert.equal(pan.mode, "sample");
  // Anything that changes the set rebuilds it: another sample, a date, a pin.
  for (const extra of [
    { sampleKey: "g0|true|2019" },
    { sampleKey: "g1|true|null" },
    { dateItems: () => ["d1"], dateKey: "Mon Apr 13 2026" },
    { pinnedItem: "popup" },
  ]) {
    const plan = planMapView(state({
      prevTier: "sampled", prevPlan: first, countInView: () => 10 ** 6, sampleKey: "g0|true|null", ...extra,
    }));
    assert.equal(plan.reused, false, JSON.stringify(extra));
    assert.notEqual(plan.desired, first.desired);
  }
  // The complete tier never reuses: its set depends on the view.
  const view = planMapView(state({ prevTier: "complete", prevPlan: { ...first, mode: "view" }, countInView: () => 5 }));
  assert.equal(view.reused, false);
});

test("drawnNoticeText makes no claim the sampled tier's own band falsifies", () => {
  // Review 1's reproduction: 1,791 panos in the visible map, 2,577 in the padded
  // area the tier is counted on, budget 2,000. The tier stays sampled, so any
  // "every pano once N or fewer are in view" would be false in this state.
  const budget = 2000;
  const plan = planMapView(state({ budget, populationSize: 59316, prevTier: "sampled", countInView: () => 2577 }));
  assert.equal(plan.tier, "sampled");
  const text = drawnNoticeText({ mode: plan.mode, drawn: budget, population: 59316, visible: null, budget });
  assert.doesNotMatch(text, /in view/);
  assert.doesNotMatch(text, /or fewer/);
  assert.equal(text, "Showing a spatial sample: 2,000 of 59,316 panoramas. Zoom in to see every panorama.");
});

test("drawnNoticeText says, in words, which state the dots are in", () => {
  const n = (v) => v.toLocaleString();
  const sample = drawnNoticeText({ mode: "sample", drawn: 40000, population: 250000, visible: null, budget: BUDGET });
  assert.match(sample, /spatial sample/);
  assert.ok(sample.includes(`${n(40000)} of ${n(250000)}`));
  assert.match(sample, /Zoom in to see every panorama/);
  const view = drawnNoticeText({ mode: "view", drawn: 1300, population: 250000, visible: 1234, budget: BUDGET });
  assert.equal(view, `Showing all ${n(1234)} panoramas in view.`);
  assert.doesNotMatch(view, /sample/);
  assert.equal(drawnNoticeText({ mode: "view", drawn: 1, population: 9, visible: 1, budget: BUDGET }),
    "Showing all 1 panorama in view.");
  assert.equal(drawnNoticeText({ mode: "all", drawn: 9, population: 9, budget: BUDGET, yearFiltered: true }),
    "Showing all 9 panoramas of this year.");
  // Under a year filter the in-view count is that year's, so the words say so
  // (review 2: "all 498 in view" while 7,195 dated panoramas were in view).
  assert.equal(drawnNoticeText({ mode: "view", drawn: 520, population: 3420, visible: 498, yearFiltered: true }),
    "Showing all 498 panoramas of this year in view.");
  assert.match(drawnNoticeText({ mode: "all", population: 50001, renderAll: true }), /all 50,001.*slow/);
  assert.match(drawnNoticeText({ mode: "hidden" }), /hidden/);
});
