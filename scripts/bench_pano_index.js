#!/usr/bin/env node
/**
 * Benchmark the city map's pano index (www/js/pano-overview.js, issue #77) as
 * it is actually built: the same buildPanoIndex, countPerCell, countInBounds
 * and idsInBounds the page calls, on seeded uniform points over a
 * Houston-sized box. Prints one JSON document (raw repeats plus medians) to
 * stdout or --out.
 *
 * Synthetic and uniform: a real city clusters on its streets, which puts more
 * points in fewer cells. Node, not a browser, so it measures the arithmetic
 * and not Leaflet; docs/experiments/city-map-viewport-dots.md says so beside
 * every number taken from it.
 *
 * @example
 *   node scripts/bench_pano_index.js --sizes 1000000,2700000,15000000 --repeats 3 \
 *     --out docs/experiments/city-map-viewport-dots_index_bench.json
 */

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { performance } = require("node:perf_hooks");

const po = require(path.join(__dirname, "..", "www", "js", "pano-overview.js"));

/** Seeded PRNG (mulberry32): the same points on every run. */
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

/** A Houston-sized box: about 44 km by 50 km around 29.76 N. */
const BOX = { south: 29.56, north: 29.96, west: -95.62, east: -95.10 };

/**
 * @param {string[]} argv
 * @returns {{sizes: number[], repeats: number, out: ?string}}
 */
function parseArgs(argv) {
  const args = { sizes: [1000000, 2700000, 15000000], repeats: 3, out: null };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--sizes") args.sizes = argv[++i].split(",").map(Number);
    else if (argv[i] === "--repeats") args.repeats = Number(argv[++i]);
    else if (argv[i] === "--out") args.out = argv[++i];
    else throw new Error(`unknown argument ${argv[i]}`);
  }
  return args;
}

/** Wall-clock milliseconds of one call, and its result. */
function timed(fn) {
  const t0 = performance.now();
  const value = fn();
  return { ms: performance.now() - t0, value };
}

function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  return s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
}

/**
 * Two views: the whole box (the fitted city view) and a street-level one
 * about 1.5 km by 1 km at the centre (roughly zoom 16 at 1440x900).
 */
function views() {
  const cLat = (BOX.south + BOX.north) / 2;
  const cLon = (BOX.west + BOX.east) / 2;
  return {
    city: { ...BOX },
    street: { south: cLat - 0.0045, north: cLat + 0.0045, west: cLon - 0.0078, east: cLon + 0.0078 },
  };
}

function benchSize(n, repeats) {
  const r = rng(77);
  const cols = po.createPanoColumns(n);
  for (let i = 0; i < n; i++) {
    cols.push(
      BOX.south + (BOX.north - BOX.south) * r(),
      BOX.west + (BOX.east - BOX.west) * r(),
      2008 + Math.floor(r() * 18),
      r() > 0.1
    );
  }
  const c = cols.finish();
  const accept = po.populationFilter(c, { officialOnly: true, year: null });
  const runs = [];
  for (let k = 0; k < repeats; k++) {
    const build = timed(() => po.buildPanoIndex(c));
    const index = build.value;
    const perCell = timed(() => po.countPerCell(index, accept));
    const v = views();
    const row = {
      build_ms: build.ms,
      count_per_cell_ms: perCell.ms,
      level: index.level,
      cells: index.cellCount,
      index_bytes: po.indexByteLength(index),
      cell_counts_bytes: perCell.value.byteLength,
    };
    for (const [name, b] of Object.entries(v)) {
      const count = timed(() => po.countInBounds(index, c, perCell.value, b, accept));
      row[`count_${name}_ms`] = count.ms;
      row[`count_${name}`] = count.value;
    }
    const ids = timed(() => po.idsInBounds(index, c, v.street, accept));
    row.ids_street_ms = ids.ms;
    row.ids_street = ids.value.length;
    runs.push(row);
  }
  const summary = {};
  for (const key of Object.keys(runs[0])) summary[key] = median(runs.map((x) => x[key]));
  return {
    n,
    columns_bytes: c.lat.byteLength + c.lon.byteLength + c.year.byteLength + c.official.byteLength,
    runs,
    median: summary,
  };
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const doc = {
    generated_by: `node scripts/bench_pano_index.js ${process.argv.slice(2).join(" ")}`.trim(),
    node: process.version,
    platform: `${os.platform()} ${os.release()} ${os.arch()}`,
    cpu: os.cpus()[0]?.model ?? "unknown",
    box: BOX,
    distribution: "seeded uniform (mulberry32, seed 77); 90% official",
    sizes: args.sizes.map((n) => benchSize(n, args.repeats)),
  };
  const text = JSON.stringify(doc, null, 1) + "\n";
  if (args.out) fs.writeFileSync(args.out, text);
  else process.stdout.write(text);
}

main();
