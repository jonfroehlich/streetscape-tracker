/**
 * pano-overview.js
 * The city map's viewport-true dot tier (issue #77): a spatial index over the
 * run's dated panoramas, and the pure planner that decides which of them are
 * drawn for the current view.
 *
 * Why this exists. Above RENDER_CAP the map used to draw ONE global spatial
 * sample, whatever the view, so zooming in never brought a dropped pano back
 * and the legend's "filter or zoom for detail" was not true. Now, on a city
 * over the cap, every pano inside the drawn area is drawn whenever they number
 * at most the budget; only a view holding more than that falls back to the old
 * sample, and the legend says which of the two the reader is looking at.
 *
 * Everything here is pure (typed arrays and plain values in, plain values
 * out; no Leaflet, no DOM), so the node suite covers it. city.js only wires it
 * up. Loaded by city.html after streetscape-utils.js and before city.js.
 *
 * The cells are the slippy-map pyramid's own cells (the tiles Leaflet draws at
 * zoom `level`), keyed by GLOBAL cell coordinates, so a cell this index reports
 * at any level from its own down to 0 is the cell `mercatorCell` gives for the
 * same point. Nothing is origin-relative, so no city's position can misalign
 * a cell, and nothing is clamped into an edge cell except what Web Mercator
 * itself cannot place (latitudes beyond about ±85.05°, and longitude 180).
 *
 * @module pano-overview
 */

// ── Cell geometry ───────────────────────────────────────────────

/** Web Mercator's latitude limit, as Leaflet's SphericalMercator uses it. */
const MERCATOR_MAX_LAT = 85.0511287798;

/**
 * Finest level the index ever keys on. A level-16 cell is a zoom-16 tile,
 * about 610 m across at the equator and 430 m at 45°N; its Morton code is
 * 32 bits, which is what lets the per-point code live in a Uint32Array.
 */
const INDEX_MAX_LEVEL = 16;

/**
 * Coarsest level the index keys on (the level used for the largest runs). A
 * zoom-14 tile is about 1.7 km across at 45°N.
 */
const INDEX_MIN_LEVEL = 14;

/**
 * Bits of a Float64 sort key that are exact integers. 2^53 is the first
 * integer a double cannot step past by one, so a key below 2^52 is exact and
 * so is every quotient and remainder by a power of two taken from it.
 */
const SORT_KEY_BITS = 52;

/**
 * Slack, in degrees, when a whole cell is classified as inside or outside a
 * view. A point lies in its cell by `floor()` of the forward projection, and
 * the cell's edges come from the inverse projection, so the two can disagree
 * by a rounding error at the edge. 1e-9° is about 0.1 mm: a cell within that
 * of the view's edge is walked point by point instead, which is exact.
 */
const CELL_EDGE_SLACK_DEG = 1e-9;

/**
 * Fractional position of a point in the unit Web Mercator square: `u` grows
 * east from longitude −180, `v` grows SOUTH from the top, both in [0, 1] for
 * points Mercator can place. The same projection Leaflet's EPSG:3857 tiles use
 * (`SphericalMercator`), so a cell here is a tile there.
 *
 * @param {number} lat - Degrees.
 * @param {number} lon - Degrees, in [−180, 180].
 * @returns {{u: number, v: number}}
 */
function mercatorUnit(lat, lon) {
  const clamped = Math.max(-MERCATOR_MAX_LAT, Math.min(MERCATOR_MAX_LAT, lat));
  const s = Math.sin((clamped * Math.PI) / 180);
  return {
    u: (lon + 180) / 360,
    v: 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI),
  };
}

/**
 * Integer cell index of a unit coordinate at a level with `n = 2^level`
 * cells per axis, kept inside [0, n − 1]. The keep-inside is what places a
 * point at longitude exactly 180 (u = 1) and a clamped polar latitude.
 *
 * @param {number} t - A unit coordinate from mercatorUnit.
 * @param {number} n - Cells per axis.
 * @returns {number}
 */
function unitToCell(t, n) {
  const c = Math.floor(t * n);
  if (c < 0) return 0;
  return c >= n ? n - 1 : c;
}

/**
 * The slippy-map cell (tile coordinates) holding a point at a zoom level.
 *
 * `x` counts east from longitude −180 and `y` counts SOUTH from the top, as
 * Leaflet's and every XYZ tile server's tile coordinates do; both are always
 * in [0, 2^level − 1], at any latitude.
 *
 * @example
 *   mercatorCell(47.6062, -122.3321, 10); // Seattle -> {x: 164, y: 357}
 *
 * @param {number} lat - Degrees.
 * @param {number} lon - Degrees, in [−180, 180].
 * @param {number} level - Zoom level, 0..30.
 * @returns {{x: number, y: number}}
 */
function mercatorCell(lat, lon, level) {
  const { u, v } = mercatorUnit(lat, lon);
  const n = 2 ** level;
  return { x: unitToCell(u, n), y: unitToCell(v, n) };
}

/**
 * The latitude/longitude box of one cell.
 *
 * @param {number} x - Cell column.
 * @param {number} y - Cell row (from the top).
 * @param {number} level - Zoom level.
 * @returns {{south: number, west: number, north: number, east: number}}
 */
function cellBounds(x, y, level) {
  const n = 2 ** level;
  const lat = (row) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * row) / n))) * 180) / Math.PI;
  return {
    south: lat(y + 1),
    west: (x / n) * 360 - 180,
    north: lat(y),
    east: ((x + 1) / n) * 360 - 180,
  };
}

/**
 * Spread the low 16 bits of `v` over the even bits of a 32-bit word.
 *
 * @param {number} v
 * @returns {number}
 */
function part1by1(v) {
  v &= 0xffff;
  v = (v | (v << 8)) & 0x00ff00ff;
  v = (v | (v << 4)) & 0x0f0f0f0f;
  v = (v | (v << 2)) & 0x33333333;
  v = (v | (v << 1)) & 0x55555555;
  return v;
}

/**
 * Gather the even bits of a 32-bit word back into 16 bits (part1by1's inverse).
 *
 * @param {number} v
 * @returns {number}
 */
function compact1by1(v) {
  v &= 0x55555555;
  v = (v | (v >>> 1)) & 0x33333333;
  v = (v | (v >>> 2)) & 0x0f0f0f0f;
  v = (v | (v >>> 4)) & 0x00ff00ff;
  v = (v | (v >>> 8)) & 0x0000ffff;
  return v;
}

/**
 * Morton (Z-order) code of a cell: x in the even bits, y in the odd bits.
 * Unsigned, in [0, 2^32). Because the bits interleave from the top, the code
 * of a cell's parent is this code shifted right by two (see mortonAtLevel),
 * so every cell at a coarser level is one contiguous run of codes.
 *
 * @param {number} x - Cell column, < 2^16.
 * @param {number} y - Cell row, < 2^16.
 * @returns {number}
 */
function mortonEncode(x, y) {
  return (part1by1(x) | (part1by1(y) << 1)) >>> 0;
}

/**
 * Cell coordinates of a Morton code (mortonEncode's inverse).
 *
 * @param {number} code
 * @returns {{x: number, y: number}}
 */
function mortonDecode(code) {
  return { x: compact1by1(code), y: compact1by1(code >>> 1) };
}

/**
 * The Morton code, at a coarser level, of the cell holding a finer cell.
 *
 * @param {number} code - Morton code at `fromLevel`.
 * @param {number} fromLevel
 * @param {number} toLevel - <= fromLevel.
 * @returns {number}
 */
function mortonAtLevel(code, fromLevel, toLevel) {
  const shift = 2 * (fromLevel - toLevel);
  // JS shifts take their count mod 32, so `code >>> 32` would be `code`.
  return shift >= 32 ? 0 : code >>> shift;
}

// ── Columns ─────────────────────────────────────────────────────

/**
 * Growable typed columns for the run's dated pano markers, appended in marker
 * order so a pano's id is its index here and in city.js's marker array.
 *
 * 19 bytes of data per pano: lat and lon as Float64, the capture YEAR as
 * Uint16 (the year filter's key), and whether the pano is official imagery
 * (the Google-only mode's key) as Uint8. The buffers grow by doubling and
 * `finish()` returns views rather than copies, so up to twice that is
 * retained: 19 to 38 bytes per pano, depending on how far the count sits past
 * a power of two. The selected capture DATE is deliberately not a column: it
 * only restyles dots and never decides which are drawn.
 *
 * @example
 *   const cols = createPanoColumns();
 *   cols.push(47.6, -122.3, 2019, true);
 *   const { lat, lon, year, official, length } = cols.finish();
 *
 * @param {number} [initialCapacity=1024]
 * @returns {{push: function(number, number, number, boolean): number,
 *   length: number, finish: function(): PanoColumns}}
 */
function createPanoColumns(initialCapacity = 1024) {
  let cap = Math.max(1, initialCapacity);
  let lat = new Float64Array(cap);
  let lon = new Float64Array(cap);
  let year = new Uint16Array(cap);
  let official = new Uint8Array(cap);
  let n = 0;
  const grow = () => {
    cap *= 2;
    const nl = new Float64Array(cap); nl.set(lat); lat = nl;
    const no = new Float64Array(cap); no.set(lon); lon = no;
    const ny = new Uint16Array(cap); ny.set(year); year = ny;
    const nf = new Uint8Array(cap); nf.set(official); official = nf;
  };
  return {
    /** Append one pano; returns its id. */
    push(la, lo, yr, isOfficial) {
      if (n === cap) grow();
      lat[n] = la;
      lon[n] = lo;
      year[n] = yr;
      official[n] = isOfficial ? 1 : 0;
      return n++;
    },
    get length() {
      return n;
    },
    /**
     * Trimmed views of the columns (no copy).
     * @returns {PanoColumns}
     */
    finish() {
      return {
        lat: lat.subarray(0, n),
        lon: lon.subarray(0, n),
        year: year.subarray(0, n),
        official: official.subarray(0, n),
        length: n,
      };
    },
  };
}

/**
 * @typedef {Object} PanoColumns
 * @property {Float64Array} lat
 * @property {Float64Array} lon
 * @property {Uint16Array} year - Capture year.
 * @property {Uint8Array} official - 1 = in mode under Google-only.
 * @property {number} length
 */

/**
 * The predicate for "this pano is in the population the map draws from": in
 * the current imagery mode and, when a year is selected, of that year. The
 * same rule as city.js's `markerInMode` plus its year filter, over columns.
 *
 * @param {PanoColumns} cols
 * @param {{officialOnly: boolean, year: ?number}} filter
 * @returns {function(number): boolean} id -> admitted
 */
function populationFilter(cols, { officialOnly, year }) {
  const { official, year: years } = cols;
  const anyYear = year == null;
  if (!officialOnly && anyYear) return () => true;
  if (!officialOnly) return (id) => years[id] === year;
  if (anyYear) return (id) => official[id] === 1;
  return (id) => official[id] === 1 && years[id] === year;
}

// ── Index ───────────────────────────────────────────────────────

/**
 * The index's key layout for `n` points.
 *
 * Bit budget: a point's sort key is `morton * 2^idBits + id`, where the Morton
 * code at level L takes 2L bits and the id takes `idBits = ceil(log2 n)`. The
 * key is exact while 2L + idBits <= SORT_KEY_BITS (52), so L is the largest
 * level that fits, kept between INDEX_MIN_LEVEL and INDEX_MAX_LEVEL:
 *
 *   n <= 2^20 (1,048,576)  -> L = 16, key < 2^52
 *   n <= 2^22 (4,194,304)  -> L = 15, key < 2^52
 *   n <= 2^24 (16,777,216) -> L = 14, key < 2^52
 *   n >  2^24              -> L = 14, and the packed key no longer fits, so
 *                             ids are sorted with a comparator instead
 *
 * @param {number} n - Point count.
 * @returns {{level: number, idBits: number, packed: boolean}}
 */
function indexLayout(n) {
  const idBits = Math.max(1, Math.ceil(Math.log2(Math.max(n, 2))));
  const fit = Math.floor((SORT_KEY_BITS - idBits) / 2);
  const level = Math.max(INDEX_MIN_LEVEL, Math.min(INDEX_MAX_LEVEL, fit));
  return { level, idBits, packed: 2 * level + idBits <= SORT_KEY_BITS };
}

/**
 * Build the spatial index: every point's cell at the index level, the points
 * ordered by cell (Morton order, then id), and the list of non-empty cells
 * with each cell's range in that order and its latitude/longitude box.
 *
 * Memory (stated because the page's limit is heap, issue #77): 8 bytes per
 * point that persist (the per-point Morton code and the order, both Uint32),
 * a transient 8 bytes per point for the sort key while building, and 44 bytes
 * per non-empty cell (Morton, start and end as Uint32, and four Float64
 * edges). The cell count is at most the point count and in practice the
 * number of level-14..16 tiles a city touches: a few thousand.
 *
 * @example
 *   const index = buildPanoIndex(cols);       // once, after the stream
 *   const accept = populationFilter(cols, { officialOnly: true, year: null });
 *   const counts = countPerCell(index, accept); // once per filter
 *   countInBounds(index, cols, counts, bounds, accept); // per view
 *
 * @param {PanoColumns} cols
 * @param {{forceComparatorSort?: boolean}} [opts] - `forceComparatorSort`
 *   takes the >2^24-point path on any input, so a test can hold the two sorts
 *   to the same order.
 * @returns {PanoIndex}
 */
function buildPanoIndex(cols, opts = {}) {
  const n = cols.length;
  const { level, idBits, packed } = indexLayout(n);
  const cellsPerAxis = 2 ** level;
  const morton = new Uint32Array(n);
  for (let id = 0; id < n; id++) {
    const { u, v } = mercatorUnit(cols.lat[id], cols.lon[id]);
    morton[id] = mortonEncode(unitToCell(u, cellsPerAxis), unitToCell(v, cellsPerAxis));
  }

  const order = new Uint32Array(n);
  if (packed && !opts.forceComparatorSort) {
    const scale = 2 ** idBits;
    const keys = new Float64Array(n);
    for (let id = 0; id < n; id++) keys[id] = morton[id] * scale + id;
    keys.sort();
    for (let i = 0; i < n; i++) order[i] = keys[i] % scale;
  } else {
    for (let id = 0; id < n; id++) order[id] = id;
    order.sort((a, b) => morton[a] - morton[b] || a - b);
  }

  let cellCount = 0;
  for (let i = 0; i < n; i++) {
    if (i === 0 || morton[order[i]] !== morton[order[i - 1]]) cellCount++;
  }
  const cellMorton = new Uint32Array(cellCount);
  const cellStart = new Uint32Array(cellCount);
  const cellEnd = new Uint32Array(cellCount);
  const cellSouth = new Float64Array(cellCount);
  const cellWest = new Float64Array(cellCount);
  const cellNorth = new Float64Array(cellCount);
  const cellEast = new Float64Array(cellCount);
  const cellAtLimit = new Uint8Array(cellCount);
  let c = -1;
  for (let i = 0; i < n; i++) {
    const code = morton[order[i]];
    if (c < 0 || code !== cellMorton[c]) {
      if (c >= 0) cellEnd[c] = i;
      c++;
      cellMorton[c] = code;
      cellStart[c] = i;
      const { x, y } = mortonDecode(code);
      const b = cellBounds(x, y, level);
      cellSouth[c] = b.south;
      cellWest[c] = b.west;
      cellNorth[c] = b.north;
      cellEast[c] = b.east;
      // The outermost rows and columns are where unitToCell keeps a point
      // that Mercator could not place, so their boxes do not bound their
      // points: such a cell is always walked point by point.
      cellAtLimit[c] = x === 0 || y === 0 || x === cellsPerAxis - 1 || y === cellsPerAxis - 1 ? 1 : 0;
    }
  }
  if (c >= 0) cellEnd[c] = n;

  return {
    level,
    length: n,
    morton,
    order,
    cellCount,
    cellMorton,
    cellStart,
    cellEnd,
    cellSouth,
    cellWest,
    cellNorth,
    cellEast,
    cellAtLimit,
  };
}

/**
 * @typedef {Object} PanoIndex
 * @property {number} level - The index level (14..16).
 * @property {number} length - Point count.
 * @property {Uint32Array} morton - Per point id: Morton code at `level`.
 * @property {Uint32Array} order - Point ids in (cell, id) order.
 * @property {number} cellCount - Non-empty cells.
 * @property {Uint32Array} cellMorton - Per cell: Morton code, ascending.
 * @property {Uint32Array} cellStart - Per cell: first position in `order`.
 * @property {Uint32Array} cellEnd - Per cell: one past the last position.
 * @property {Float64Array} cellSouth
 * @property {Float64Array} cellWest
 * @property {Float64Array} cellNorth
 * @property {Float64Array} cellEast
 * @property {Uint8Array} cellAtLimit - 1 = on the pyramid's outer edge.
 */

/**
 * The cell the index places a point in, at the index level or any coarser
 * level. Read from the index's own per-point Morton code, so a test can hold
 * it to `mercatorCell` for every level.
 *
 * @param {PanoIndex} index
 * @param {number} id - Point id.
 * @param {number} level - 0..index.level.
 * @returns {{x: number, y: number}}
 */
function indexCellOf(index, id, level) {
  if (level > index.level || level < 0) {
    throw new RangeError(`level ${level} is outside 0..${index.level}`);
  }
  return mortonDecode(mortonAtLevel(index.morton[id], index.level, level));
}

/**
 * Bytes the index holds (typed arrays only), for the measurement writeup.
 *
 * @param {PanoIndex} index
 * @returns {number}
 */
function indexByteLength(index) {
  return [
    index.morton, index.order, index.cellMorton, index.cellStart, index.cellEnd,
    index.cellSouth, index.cellWest, index.cellNorth, index.cellEast, index.cellAtLimit,
  ].reduce((sum, a) => sum + a.byteLength, 0);
}

/**
 * How many admitted points each cell holds: one pass over the points, redone
 * only when the population filter changes (mode or year), never per view.
 * One Uint32 per non-empty cell, so its size is bounded by the cell count.
 *
 * @param {PanoIndex} index
 * @param {function(number): boolean} accept - From populationFilter.
 * @returns {Uint32Array} Per cell count.
 */
function countPerCell(index, accept) {
  const counts = new Uint32Array(index.cellCount);
  const { order, cellStart, cellEnd } = index;
  for (let c = 0; c < index.cellCount; c++) {
    let k = 0;
    for (let i = cellStart[c]; i < cellEnd[c]; i++) if (accept(order[i])) k++;
    counts[c] = k;
  }
  return counts;
}

/**
 * Where a cell sits against a view: entirely inside (1), entirely outside
 * (−1), or across its edge (0), with CELL_EDGE_SLACK_DEG of slack so a cell
 * is only called inside or outside when every point in it certainly is.
 *
 * @param {PanoIndex} index
 * @param {number} c - Cell.
 * @param {{south: number, west: number, north: number, east: number}} b
 * @returns {number}
 */
function classifyCell(index, c, b) {
  const e = CELL_EDGE_SLACK_DEG;
  const s = index.cellSouth[c] - e;
  const n = index.cellNorth[c] + e;
  const w = index.cellWest[c] - e;
  const ea = index.cellEast[c] + e;
  if (n < b.south || s > b.north || ea < b.west || w > b.east) {
    return index.cellAtLimit[c] ? 0 : -1;
  }
  if (!index.cellAtLimit[c] && s >= b.south && n <= b.north && w >= b.west && ea <= b.east) {
    return 1;
  }
  return 0;
}

/**
 * Whether a point lies in a view (edges inclusive, as Leaflet's
 * `LatLngBounds.contains` treats them).
 *
 * @param {number} lat
 * @param {number} lon
 * @param {{south: number, west: number, north: number, east: number}} b
 * @returns {boolean}
 */
function inBounds(lat, lon, b) {
  return lat >= b.south && lat <= b.north && lon >= b.west && lon <= b.east;
}

/**
 * EXACTLY how many admitted points lie in a view: whole cells inside the view
 * are summed from `cellCounts`, and only the cells across its edge are walked
 * point by point. Memory: none beyond the inputs.
 *
 * @param {PanoIndex} index
 * @param {PanoColumns} cols
 * @param {Uint32Array} cellCounts - From countPerCell with the same `accept`.
 * @param {{south: number, west: number, north: number, east: number}} bounds
 * @param {function(number): boolean} accept
 * @returns {number}
 */
function countInBounds(index, cols, cellCounts, bounds, accept) {
  const { order, cellStart, cellEnd } = index;
  const { lat, lon } = cols;
  let total = 0;
  for (let c = 0; c < index.cellCount; c++) {
    const where = classifyCell(index, c, bounds);
    if (where === 1) {
      total += cellCounts[c];
    } else if (where === 0) {
      for (let i = cellStart[c]; i < cellEnd[c]; i++) {
        const id = order[i];
        if (accept(id) && inBounds(lat[id], lon[id], bounds)) total++;
      }
    }
  }
  return total;
}

/**
 * The ids of EXACTLY the admitted points in a view, in index order.
 *
 * @param {PanoIndex} index
 * @param {PanoColumns} cols
 * @param {{south: number, west: number, north: number, east: number}} bounds
 * @param {function(number): boolean} accept
 * @returns {number[]}
 */
function idsInBounds(index, cols, bounds, accept) {
  const { order, cellStart, cellEnd } = index;
  const { lat, lon } = cols;
  const out = [];
  for (let c = 0; c < index.cellCount; c++) {
    const where = classifyCell(index, c, bounds);
    if (where < 0) continue;
    for (let i = cellStart[c]; i < cellEnd[c]; i++) {
      const id = order[i];
      if (!accept(id)) continue;
      if (where === 1 || inBounds(lat[id], lon[id], bounds)) out.push(id);
    }
  }
  return out;
}

// ── The view planner ────────────────────────────────────────────

/**
 * The every-pano tier, once entered, is left only when the drawn area holds
 * more than this multiple of the budget. Without the band a few pixels of pan
 * across the threshold would swap every dot for the sample and back, and
 * announce each swap.
 */
const TIER_LEAVE_FACTOR = 1.25;

/**
 * Words for the polite live region, said once per actual tier change.
 */
const TIER_ANNOUNCEMENTS = {
  complete: "Every panorama in view is now drawn.",
  sampled: "Too many panoramas in view to draw them all: the map now shows a spatial sample. "
    + "Zoom in to see every panorama.",
};

/**
 * Whether the drawn set depends on the view at all. False exactly when the
 * population fits the budget (every city at or under RENDER_CAP, and a larger
 * city under a year filter that fits) or "Render all" is on: those take the
 * pre-#77 path, which draws the whole population and never counts a view.
 *
 * @param {{populationSize: number, budget: number, renderAll: boolean}} s
 * @returns {boolean}
 */
function isViewDependent({ populationSize, budget, renderAll }) {
  return !renderAll && populationSize > budget;
}

/**
 * Decide what the pano layer draws. Pure: the expensive inputs are thunks, so
 * a plan only pays for what its branch needs, and a test can assert that the
 * under-cap path never counts a view.
 *
 * Tiers. "complete" means every population pano in the drawn area is drawn;
 * "sampled" means the area holds too many, so the old global spatial sample
 * is drawn. Modes say how: "all" (the population fits, or Render all: draw it
 * whole, the pre-#77 path), "view" (complete, from the view), "sample", and
 * "hidden" (the Panoramas layer is off: nothing is drawn and the tier is left
 * as it was, so turning it back on announces nothing).
 *
 * The tier rule, counted over the same padded bounds that are drawn: enter
 * "complete" at a count <= budget, and once in it stay until the count passes
 * TIER_LEAVE_FACTOR × budget. A plan with `freezeTier` (the move was a
 * popup's own auto-pan) keeps the previous tier whatever the count.
 *
 * The selected capture date never decides the tier or narrows the set: its
 * panos are unioned in, as before #77, so the map cannot under-show the
 * date the chart reports, and city.js only dims the rest.
 *
 * An open popup's pano (`pinnedItem`) is kept drawn on EVERY re-plan, whatever
 * caused it (a move, a date, the flat toggle, Render all), because removing a
 * Leaflet marker closes its popup. It is dropped only when it has left the
 * population (`inPopulation` is false: a year or imagery mode that excludes
 * it), which is a filter the reader chose, and when the layer is off.
 *
 * A re-plan that lands in the sampled tier again, on the same sample
 * (`sampleKey`), date (`dateKey`) and pin, reuses the previous plan's set
 * (`reused: true`) rather than rebuilding and re-diffing the same 40,000
 * markers on every pan; `sampleItems` and `dateItems` are then not called.
 *
 * Flat-only points are not pano dots and never enter `desired`; `flatShown`
 * is the flat layer's own switch and ignores the Panoramas layer.
 *
 * @example
 *   const plan = planMapView({
 *     budget: 40000, populationSize: 250000, renderAll: false,
 *     panosHidden: false, prevTier: "sampled", freezeTier: false, flatOn: false,
 *     populationItems: () => all, sampleItems: () => sample,
 *     countInView: () => 31000, viewItems: () => inView,
 *     dateItems: null, pinnedItem: null,
 *   });
 *   // plan.tier === "complete", plan.mode === "view",
 *   // plan.announce === "Every panorama in view is now drawn."
 *
 * @param {Object} s
 * @param {number} s.budget - Most dots drawn at once (RENDER_CAP).
 * @param {number} s.populationSize - Panos in the current mode and year.
 * @param {boolean} s.renderAll - The legend's "Render all" override.
 * @param {boolean} s.panosHidden - The Panoramas layer is off.
 * @param {?string} s.prevTier - The previous plan's tier, or null at first.
 * @param {boolean} s.freezeTier - Keep prevTier (a popup auto-pan moved the map).
 * @param {boolean} s.flatOn - The flat-only toggle.
 * @param {function(): Iterable} s.populationItems - The whole population.
 * @param {function(): Iterable} s.sampleItems - The spatial-stride sample.
 * @param {function(): number} s.countInView - Population count in the drawn area.
 * @param {function(): Iterable} s.viewItems - Population in the drawn area.
 * @param {?function(): Iterable} s.dateItems - The selected date's in-mode
 *   panos, or null when no date is selected.
 * @param {*} [s.pinnedItem] - An open popup's pano, kept drawn; or null.
 * @param {function(*): boolean} [s.inPopulation] - Whether the pinned pano is
 *   still in the population (mode and year); absent = always.
 * @param {?Object} [s.prevPlan] - The plan the map shows now, or null.
 * @param {string} [s.sampleKey] - Names the sample `sampleItems` returns (the
 *   filter, and which build of the caches); equal keys mean the same sample.
 * @param {?string} [s.dateKey] - Names the selected date, or null.
 * @returns {{mode: string, tier: ?string, desired: Set, inViewCount: ?number,
 *   announce: ?string, flatShown: boolean, reused: boolean, sampleKey: ?string,
 *   dateKey: ?string, pinnedItem: *}}
 */
function planMapView(s) {
  const flatShown = Boolean(s.flatOn);
  const dateKey = s.dateKey ?? null;
  if (s.panosHidden) {
    return {
      mode: "hidden", tier: s.prevTier, desired: new Set(), inViewCount: null, announce: null,
      flatShown, reused: false, sampleKey: null, dateKey, pinnedItem: null,
    };
  }
  const pinnedItem = s.pinnedItem != null && (!s.inPopulation || s.inPopulation(s.pinnedItem))
    ? s.pinnedItem : null;

  let mode;
  let tier;
  let inViewCount = null;
  let desired;
  if (!isViewDependent(s)) {
    mode = "all";
    tier = "complete";
    desired = new Set(s.populationItems());
  } else {
    inViewCount = s.countInView();
    if (s.freezeTier && s.prevTier) {
      tier = s.prevTier;
    } else {
      const limit = s.prevTier === "complete" ? Math.floor(s.budget * TIER_LEAVE_FACTOR) : s.budget;
      tier = inViewCount <= limit ? "complete" : "sampled";
    }
    mode = tier === "complete" ? "view" : "sample";
  }

  const sampleKey = mode === "sample" ? (s.sampleKey ?? null) : null;
  const p = s.prevPlan;
  const reused = Boolean(
    mode === "sample" && p && p.mode === "sample" && sampleKey != null
      && p.sampleKey === sampleKey && p.dateKey === dateKey && p.pinnedItem === pinnedItem
  );
  if (reused) {
    desired = p.desired;
  } else {
    if (mode === "view") desired = new Set(s.viewItems());
    else if (mode === "sample") desired = new Set(s.sampleItems());
    if (s.dateItems) for (const item of s.dateItems()) desired.add(item);
    if (pinnedItem != null) desired.add(pinnedItem);
  }

  const changed = s.prevTier != null && tier !== s.prevTier && !s.freezeTier;
  return {
    mode,
    tier,
    desired,
    inViewCount,
    announce: changed ? TIER_ANNOUNCEMENTS[tier] : null,
    flatShown,
    reused,
    sampleKey,
    dateKey,
    pinnedItem,
  };
}

/**
 * The legend's line about what the pano layer is drawing, true in every
 * state. Shown only for a city over RENDER_CAP (city.js decides that).
 *
 * @example
 *   drawnNoticeText({ mode: "sample", drawn: 40000, population: 250000,
 *                     visible: null, yearFiltered: false });
 *   // "Showing a spatial sample: 40,000 of 250,000 panoramas. Zoom in to see
 *   //  every panorama."
 *
 * @param {Object} s
 * @param {string} s.mode - A plan's mode.
 * @param {number} s.drawn - Dots drawn (the desired set's size).
 * @param {number} s.population - Panos in the current mode and year.
 * @param {?number} s.visible - Population panos inside the visible map (not
 *   the padded drawn area), for the "view" mode.
 * @param {boolean} s.yearFiltered - A year filter is active.
 * @param {boolean} [s.renderAll]
 * @returns {string}
 */
function drawnNoticeText(s) {
  const f = (v) => Number(v).toLocaleString();
  if (s.mode === "hidden") return "Panorama dots are hidden (the Panoramas layer is off).";
  if (s.renderAll) return `Drawing all ${f(s.population)} panoramas; this can be slow.`;
  if (s.mode === "all") {
    return `Showing all ${f(s.population)} panoramas${s.yearFiltered ? " of this year" : ""}.`;
  }
  if (s.mode === "view") {
    const n = s.visible ?? s.drawn;
    return `Showing all ${f(n)} panorama${n === 1 ? "" : "s"}${s.yearFiltered ? " of this year" : ""} in view.`;
  }
  // No threshold is quoted: the tier is counted over the drawn area (the view
  // plus a margin) with hysteresis, so "N or fewer in view" would be false for
  // a view holding fewer than N that is still sampled.
  return `Showing a spatial sample: ${f(s.drawn)} of ${f(s.population)} panoramas. `
    + "Zoom in to see every panorama.";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    MERCATOR_MAX_LAT,
    INDEX_MAX_LEVEL,
    INDEX_MIN_LEVEL,
    TIER_LEAVE_FACTOR,
    TIER_ANNOUNCEMENTS,
    mercatorCell,
    cellBounds,
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
  };
}
