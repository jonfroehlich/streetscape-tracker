// Offline unit tests for the pure helpers in street-coverage.js (issue #24).
// Run with `npm test` (Node's built-in test runner) — no network, no jsdom.
// In the browser these helpers read shared globals from streetscape-utils.js;
// here we stub just the two they touch (STREETSCAPE_DATA_BASE_URL, getColor).

const test = require("node:test");
const assert = require("node:assert/strict");

global.STREETSCAPE_DATA_BASE_URL = "https://example.test/data/";
global.getColor = (age, provider) => `color(${age},${provider})`;
// The legend-section builders (issue #104) also read these streetscape-utils.js
// globals; minimal stand-ins with the real escaping contract.
global.escapeHtml = (s) =>
  String(s).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );
global.PROVIDERS = { gsv: { label: "Google Street View" }, mapillary: { label: "Mapillary" } };
global.streetNetworkLabel = (n) => ({ drive: "Roads", all_public: "Roads + paths" })[n] ?? n;

const {
  streetsUrlForDataFile,
  styleStreetFeature,
  styleStreetByCoverage,
  styleStreetByType,
  styleForMode,
  streetTypeColor,
  streetTypeOrder,
  isNonMotorizedType,
  typeLegendGroups,
  STREET_TYPE_COLORS,
  withStreetAlpha,
  fractionColor,
  normalizeStreetArtifact,
  renderStreetCoverage,
  streetLegendChipsHtml,
  streetLegendSectionHtml,
  panoVisibilityLayer,
  STREET_PARTIAL_LOW_COLOR,
  STREET_PARTIAL_MID_COLOR,
  STREET_UNCOVERED_COLOR,
  STREET_COVERED_COLOR,
  STREET_COVERED_NODATE_COLOR,
  STREET_TYPE_MINOR_COLOR,
  STREET_GAP_HIGHLIGHT_COLOR,
  styleWithGapHighlight,
  orderedStreetTypes,
  labelTextColorFor,
  chartBarBaseColor,
} = require("../street-coverage.js");

test("streetsUrlForDataFile swaps .csv.gz for _streets.json.gz under the data base URL", () => {
  // Mirrors naming.streets_filename_for_run on the Python side — keep in sync.
  assert.equal(
    streetsUrlForDataFile("bend--or_width_5000_height_5000_step_20_2026-07-08.csv.gz"),
    "https://example.test/data/bend--or_width_5000_height_5000_step_20_2026-07-08_streets.json.gz"
  );
  // Provider-tagged run filenames keep their token.
  assert.equal(
    streetsUrlForDataFile("bend--or_width_5000_height_5000_step_20_mapillary_2026-07-08.csv.gz"),
    "https://example.test/data/bend--or_width_5000_height_5000_step_20_mapillary_2026-07-08_streets.json.gz"
  );
});

test("streetsUrlForDataFile throws on a non-.csv.gz filename (mirrors the Python contract)", () => {
  // Without the suffix guard the regex replace is a no-op and we'd fetch the
  // wrong URL; match naming.streets_filename_for_run and throw instead.
  assert.throws(() => streetsUrlForDataFile("bend--or_streets.json.gz"), /Not a run csv\.gz/);
  assert.throws(() => streetsUrlForDataFile("bend--or.csv"), /Not a run csv\.gz/);
});

test("styleStreetFeature: uncovered segments are gray and dashed", () => {
  const style = styleStreetFeature({ properties: { covered: false } }, "gsv");
  assert.equal(style.color, STREET_UNCOVERED_COLOR);
  assert.equal(style.dashArray, "4 4");
});

test("styleStreetFeature: covered segment without a date uses the fallback color", () => {
  const style = styleStreetFeature(
    { properties: { covered: true, nearest_pano_age_years: null } },
    "gsv"
  );
  assert.equal(style.color, STREET_COVERED_NODATE_COLOR);
  assert.equal(style.dashArray, undefined);
});

test("styleStreetFeature: covered segment with an age uses the provider age scale", () => {
  const style = styleStreetFeature(
    { properties: { covered: true, nearest_pano_age_years: 3.2 } },
    "mapillary"
  );
  assert.equal(style.color, "color(3.2,mapillary)");
});

test("styleStreetByCoverage: binary covered green vs uncovered slate (dashed)", () => {
  assert.equal(
    styleStreetByCoverage({ properties: { covered: true } }).color,
    STREET_COVERED_COLOR
  );
  const uncovered = styleStreetByCoverage({ properties: { covered: false } });
  assert.equal(uncovered.color, STREET_UNCOVERED_COLOR);
  assert.equal(uncovered.dashArray, "4 4");
});

test("styleStreetByType: colors by highway class; uncovered is faded + dashed", () => {
  const covered = styleStreetByType({ properties: { covered: true, highway: "residential" } });
  assert.equal(covered.color, streetTypeColor("residential"));
  assert.equal(covered.dashArray, undefined);

  const uncovered = styleStreetByType({ properties: { covered: false, highway: "residential" } });
  assert.equal(uncovered.color, streetTypeColor("residential")); // keeps its type hue
  assert.equal(uncovered.dashArray, "4 4");
  assert.ok(uncovered.opacity < covered.opacity); // but faded
});

test("streetTypeColor: unlisted classes fold into the neutral minor color", () => {
  assert.equal(streetTypeColor("motorway"), "#3987e5");
  assert.equal(streetTypeColor("living_street"), STREET_TYPE_MINOR_COLOR);
  assert.equal(streetTypeColor("other"), STREET_TYPE_MINOR_COLOR);
});

test("streetTypeColor: service subtypes inherit the service hue, not a new one", () => {
  // alley/driveway/parking_aisle are all highway=service; the analyzer splits
  // them, but they are one visual family. The palette must stay at 8 hues.
  for (const subtype of ["alley", "driveway", "parking_aisle"]) {
    assert.equal(streetTypeColor(subtype), STREET_TYPE_COLORS.service);
  }
  assert.equal(Object.keys(STREET_TYPE_COLORS).length, 8);
});

test("streetTypeColor: non-motorized classes take the minor gray, no new hue", () => {
  for (const cls of ["footway", "path", "pedestrian", "cycleway", "steps", "track", "bridleway"]) {
    assert.equal(streetTypeColor(cls), STREET_TYPE_MINOR_COLOR);
    assert.ok(isNonMotorizedType(cls));
  }
  assert.ok(!isNonMotorizedType("residential"));
  assert.ok(!isNonMotorizedType("alley")); // an alley is a drivable back street
});

test("styleStreetByType: non-motorized ways draw thinner than roads", () => {
  // Gray is shared with living_street/other, so thickness is what separates a
  // footpath from an unhued road class. Dash and opacity are already taken by
  // covered/uncovered and the spotlight.
  const road = styleStreetByType({ properties: { covered: true, highway: "residential" } });
  const foot = styleStreetByType({ properties: { covered: true, highway: "footway" } });
  assert.ok(foot.weight < road.weight);
  assert.equal(foot.dashArray, undefined); // still reads as covered

  const footUncovered = styleStreetByType({ properties: { covered: false, highway: "footway" } });
  const roadUncovered = styleStreetByType({ properties: { covered: false, highway: "residential" } });
  assert.ok(footUncovered.weight < roadUncovered.weight);
  assert.equal(footUncovered.dashArray, "4 4");
});

test("streetTypeOrder: importance rank, unlisted classes sort last", () => {
  assert.ok(streetTypeOrder("motorway") < streetTypeOrder("residential"));
  assert.ok(streetTypeOrder("residential") < streetTypeOrder("other"));
});

test("streetTypeOrder: roads, then the service family, then non-motorized", () => {
  assert.ok(streetTypeOrder("residential") < streetTypeOrder("service"));
  assert.ok(streetTypeOrder("service") < streetTypeOrder("alley"));
  assert.ok(streetTypeOrder("alley") < streetTypeOrder("footway"));
  assert.ok(streetTypeOrder("footway") < streetTypeOrder("bridleway"));
  // living_street is a motorized road class and ranks with them, immediately
  // after service — matching _BUCKET_DISPLAY_ORDER, which is the order the
  // artifact's own coverage_by_highway keys come in. Only "other" sinks.
  assert.ok(streetTypeOrder("service") < streetTypeOrder("living_street"));
  assert.ok(streetTypeOrder("living_street") < streetTypeOrder("alley"));
  assert.ok(streetTypeOrder("bridleway") < streetTypeOrder("other"));
});

// --- typeLegendGroups ------------------------------------------------------

test("typeLegendGroups: one entry per rendered style, not per class", () => {
  // A broad-network walk carries up to ten classes but the map draws them in
  // two colors (service subtypes share the service hue; non-motorized ways all
  // share the minor gray, thinner). Ten labels against two swatches reads as a
  // broken palette — merging them says what the map actually does.
  const groups = typeLegendGroups([
    "footway",
    "alley",
    "residential",
    "path",
    "driveway",
    "service",
  ]);
  assert.deepEqual(
    groups.map((g) => g.labels),
    [["residential"], ["service", "alley", "driveway"], ["footway", "path"]]
  );
  // Every entry in a group renders identically, which is why they merged.
  assert.equal(groups[1].color, streetTypeColor("service"));
  assert.equal(groups[1].thin, false);
  assert.equal(groups[2].color, STREET_TYPE_MINOR_COLOR);
  assert.equal(groups[2].thin, true);
});

test("typeLegendGroups: groups follow the artifact's own class order", () => {
  const groups = typeLegendGroups(["bridleway", "motorway", "alley"]);
  assert.deepEqual(
    groups.map((g) => g.labels[0]),
    ["motorway", "alley", "bridleway"]
  );
});

test("typeLegendGroups: thickness splits the classes that share the minor gray", () => {
  // living_street and "other" are gray but NOT thin, so they merge with each
  // other; folding the footpaths in too would claim a visual equivalence the
  // map does not draw (styleStreetByType renders those a step thinner).
  const groups = typeLegendGroups(["living_street", "footway", "other"]);
  assert.deepEqual(
    groups.map((g) => g.labels),
    [["living_street", "other"], ["footway"]]
  );
  assert.equal(groups[0].thin, false);
  assert.equal(groups[1].thin, true);
  assert.equal(groups[0].color, groups[1].color);
});

test("styleForMode: dispatches to the right per-mode styler", () => {
  const feat = { properties: { covered: true, highway: "primary", nearest_pano_age_years: 1 } };
  assert.equal(styleForMode(feat, "coverage", "gsv").color, STREET_COVERED_COLOR);
  assert.equal(styleForMode(feat, "type", "gsv").color, streetTypeColor("primary"));
  assert.equal(styleForMode(feat, "age", "gsv").color, "color(1,gsv)"); // stubbed getColor
});

test("withStreetAlpha: hex to rgba() with the given alpha", () => {
  assert.equal(withStreetAlpha("#2fb974", 0.22), "rgba(47, 185, 116, 0.22)");
});

// ── Fractional coverage (road-walk / streetwalk artifact, #99/#155) ──────────

test("fractionColor: 0 is the pale end, 1 is the full covered green, monotonic between", () => {
  // Endpoints are the ramp anchors exactly (3-stop ramp: low → mid → covered).
  assert.equal(fractionColor(0), "rgb(226, 245, 234)"); // STREET_PARTIAL_LOW_COLOR #e2f5ea
  assert.equal(fractionColor(0.5), "rgb(124, 207, 159)"); // STREET_PARTIAL_MID_COLOR #7ccf9f
  assert.equal(fractionColor(1), "rgb(47, 185, 116)"); // STREET_COVERED_COLOR #2fb974
  // Green channel decreases as fraction rises (245 → 207 → 185): monotonicity.
  const g = (c) => Number(c.match(/rgb\(\d+, (\d+),/)[1]);
  assert.ok(g(fractionColor(0)) > g(fractionColor(0.25)));
  assert.ok(g(fractionColor(0.25)) > g(fractionColor(0.5)));
  assert.ok(g(fractionColor(0.5)) > g(fractionColor(0.75)));
  assert.ok(g(fractionColor(0.75)) > g(fractionColor(1)));
  // Out-of-range clamps rather than extrapolating.
  assert.equal(fractionColor(-1), fractionColor(0));
  assert.equal(fractionColor(2), fractionColor(1));
});

test("styleStreetByCoverage: fractional artifact graduates covered edges by coverage_fraction", () => {
  const partial = styleStreetByCoverage({ properties: { covered: true, coverage_fraction: 0.5 } });
  assert.equal(partial.color, fractionColor(0.5));
  const full = styleStreetByCoverage({ properties: { covered: true, coverage_fraction: 1 } });
  assert.equal(full.color, fractionColor(1)); // fraction 1 == the covered green (as rgb())
  // Uncovered is still slate + dashed regardless of the fractional signal.
  const none = styleStreetByCoverage({ properties: { covered: false, coverage_fraction: 0 } });
  assert.equal(none.color, STREET_UNCOVERED_COLOR);
  assert.equal(none.dashArray, "4 4");
  // A grid feature (no coverage_fraction) keeps the binary green.
  assert.equal(
    styleStreetByCoverage({ properties: { covered: true } }).color,
    STREET_COVERED_COLOR
  );
});

test("normalizeStreetArtifact: streetwalk aliases age + totals keys and flags fractional", () => {
  const fc = {
    properties: {
      metadata: {
        totals: {
          edges: 41,
          edges_any_coverage: 40,
          coverage_pct_by_length: 95.6,
          uncovered_pct_by_length: 4.4,
        },
        coverage_by_highway: {},
      },
    },
    features: [
      { properties: { covered: true, coverage_fraction: 0.8, median_covered_age_years: 2.5 } },
      { properties: { covered: false, coverage_fraction: 0 } },
    ],
  };
  const { meta, hasFractional } = normalizeStreetArtifact(fc, "streetwalk");
  assert.equal(hasFractional, true);
  // Age alias so the styler's `nearest_pano_age_years` path works.
  assert.equal(fc.features[0].properties.nearest_pano_age_years, 2.5);
  // Totals aliases for the panel headline.
  assert.equal(meta.totals.segments, 41);
  assert.equal(meta.totals.covered, 40);
});

test("normalizeStreetArtifact: grid artifact is untouched and not flagged fractional", () => {
  const fc = {
    properties: { metadata: { totals: { segments: 10, covered: 7 }, coverage_by_highway: {} } },
    features: [{ properties: { covered: true, nearest_pano_age_years: 1.0 } }],
  };
  const { meta, hasFractional } = normalizeStreetArtifact(fc, "grid");
  assert.equal(hasFractional, false);
  assert.equal(fc.features[0].properties.nearest_pano_age_years, 1.0);
  assert.equal(meta.totals.segments, 10);
  assert.equal(meta.totals.covered, 7);
});

// NOTE: lookupStreetwalk / fetchStreetwalkManifest moved to
// streetscape-utils.js (the overview map and streets.html need them too);
// their tests moved with them to streetscape-utils.test.js.

// ── normalizeStreetArtifact edge cases ───────────────────────────────────────

test("normalizeStreetArtifact: does not clobber values the artifact already carries", () => {
  const fc = {
    properties: {
      metadata: {
        // A streetwalk artifact that already speaks the canonical totals names
        // (e.g. a future schema rev) must keep its own numbers.
        totals: { edges: 41, edges_any_coverage: 40, segments: 7, covered: 5 },
        coverage_by_highway: {},
      },
    },
    features: [
      {
        properties: {
          covered: true,
          coverage_fraction: 0.5,
          nearest_pano_age_years: 1.5, // already present → alias must not overwrite
          median_covered_age_years: 9.9,
        },
      },
    ],
  };
  const { meta } = normalizeStreetArtifact(fc, "streetwalk");
  assert.equal(fc.features[0].properties.nearest_pano_age_years, 1.5);
  assert.equal(meta.totals.segments, 7);
  assert.equal(meta.totals.covered, 5);
});

test("normalizeStreetArtifact: a covered edge with no median age aliases to null, not undefined", () => {
  // The styler branches on `nearest_pano_age_years == null` for the no-date
  // color; an undefined would take the same branch today but null is the
  // contract the grid artifact uses, so keep them identical.
  const fc = {
    properties: { metadata: { totals: { edges: 1 }, coverage_by_highway: {} } },
    features: [{ properties: { covered: true, coverage_fraction: 1 } }],
  };
  normalizeStreetArtifact(fc, "streetwalk");
  assert.equal(fc.features[0].properties.nearest_pano_age_years, null);
  assert.ok("nearest_pano_age_years" in fc.features[0].properties);
});

test("normalizeStreetArtifact: tolerates features with no properties and a missing feature list", () => {
  const fc = { properties: { metadata: { totals: {}, coverage_by_highway: {} } }, features: [{}] };
  assert.doesNotThrow(() => normalizeStreetArtifact(fc, "streetwalk"));
  assert.deepEqual(fc.features[0].properties, { nearest_pano_age_years: null });

  const empty = {};
  const { meta, hasFractional } = normalizeStreetArtifact(empty, "streetwalk");
  assert.equal(hasFractional, false);
  assert.equal(meta, undefined);
});

test("normalizeStreetArtifact: a streetwalk artifact with no fractional signal is not flagged", () => {
  // hasFractional drives the initial view mode; an artifact whose edges lack
  // coverage_fraction must fall back to the age scale like the grid file.
  const fc = {
    properties: { metadata: { totals: { edges: 2 }, coverage_by_highway: {} } },
    features: [{ properties: { covered: true, median_covered_age_years: 3 } }],
  };
  assert.equal(normalizeStreetArtifact(fc, "streetwalk").hasFractional, false);
});

// ── renderStreetCoverage: artifact discovery + initial mode ──────────────────
//
// The chart is skipped in these tests (buildStreetCoverageChart early-returns
// when #street-chart-container is absent), so they exercise exactly the
// fetch/normalize/style/layer-control seam without needing a DOM or Chart.js.

/** Minimal Leaflet + DOM stubs; returns a handle on what the renderer built. */
function stubRenderEnv(fetchImpl) {
  const captured = { urls: [], geoJsonOpts: null, added: 0, styleFn: null };
  captured.controlEl = {
    attrs: {},
    setAttribute(k, v) {
      this.attrs[k] = v;
    },
  };
  global.fetchGzippedJson = async (url) => {
    captured.urls.push(url);
    return fetchImpl(url);
  };
  global.document = { getElementById: () => null };
  global.L = {
    geoJSON: (fc, opts) => {
      captured.geoJsonOpts = opts;
      captured.fc = fc;
      const layer = {
        addTo: () => {
          captured.added += 1;
          return layer;
        },
        setStyle: (fn) => {
          captured.styleFn = fn;
        },
      };
      captured.layer = layer;
      return layer;
    },
    control: {
      layers: (base, overlays, opts) => {
        captured.layersControl = { base, overlays, opts };
        return { addTo: () => ({ getContainer: () => captured.controlEl }) };
      },
    },
    layerGroup: () => {
      const handlers = {};
      return {
        on: (ev, fn) => (handlers[ev] = fn),
        handlers,
        addTo() {
          return this;
        },
      };
    },
  };
  const panes = {};
  captured.map = {
    getPane: (n) => panes[n],
    createPane: (n) => (panes[n] = { style: {} }),
  };
  return captured;
}

function teardownRenderEnv() {
  delete global.fetchGzippedJson;
  delete global.document;
  delete global.L;
}

const GRID_RUN = "bend--or_width_5000_height_5000_step_20_2026-07-08.csv.gz";
const WALK_FILE = "bend--or_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-22_coverage.json.gz";

function streetwalkArtifact() {
  return {
    properties: {
      metadata: {
        totals: {
          edges: 2,
          edges_any_coverage: 2,
          coverage_pct_by_length: 98.4,
          uncovered_pct_by_length: 1.6,
        },
        coverage_by_highway: { residential: { length_km: 1 } },
      },
    },
    features: [
      {
        properties: {
          highway: "residential",
          covered: true,
          coverage_fraction: 0.42,
          median_covered_age_years: 3.5,
          nearest_pano_date: "2022-06",
        },
      },
    ],
  };
}

function gridArtifact() {
  return {
    properties: {
      metadata: {
        totals: {
          segments: 2,
          covered: 1,
          coverage_pct_by_length: 88.0,
          uncovered_pct_by_length: 12.0,
        },
        coverage_by_highway: { residential: { length_km: 1 } },
      },
    },
    features: [
      {
        properties: {
          highway: "residential",
          covered: true,
          nearest_pano_age_years: 3.5,
          nearest_pano_date: "2022-06",
        },
      },
    ],
  };
}

test("renderStreetCoverage: with a manifest filename, fetches THAT artifact — not the derived sibling", async () => {
  // The whole point of the manifest (#155): the streetwalk file's sp{N} spacing
  // and run-date are not derivable from the grid run filename.
  const env = stubRenderEnv(() => streetwalkArtifact());
  await renderStreetCoverage(env.map, GRID_RUN, "gsv", { streetwalkFile: WALK_FILE });
  assert.deepEqual(env.urls, ["https://example.test/data/" + WALK_FILE]);
  assert.equal(env.added, 1);
  teardownRenderEnv();
});

test("renderStreetCoverage: with no manifest entry, falls back to the derived _streets.json.gz", async () => {
  const env = stubRenderEnv(() => gridArtifact());
  await renderStreetCoverage(env.map, GRID_RUN, "gsv", {});
  assert.deepEqual(env.urls, [streetsUrlForDataFile(GRID_RUN)]);
  assert.equal(env.added, 1);
  teardownRenderEnv();
});

test("renderStreetCoverage: the fractional artifact opens on the coverage ramp", async () => {
  // Observable through the style callback handed to L.geoJSON: in "coverage"
  // mode a covered edge takes the fraction ramp color, not the age color.
  const env = stubRenderEnv(() => streetwalkArtifact());
  await renderStreetCoverage(env.map, GRID_RUN, "gsv", { streetwalkFile: WALK_FILE });
  const style = env.geoJsonOpts.style(env.fc.features[0]);
  assert.equal(style.color, fractionColor(0.42));
  teardownRenderEnv();
});

test("renderStreetCoverage: the binary grid artifact opens on the age scale", async () => {
  const env = stubRenderEnv(() => gridArtifact());
  await renderStreetCoverage(env.map, GRID_RUN, "gsv", {});
  const style = env.geoJsonOpts.style(env.fc.features[0]);
  assert.equal(style.color, "color(3.5,gsv)"); // the getColor stub → age mode
  teardownRenderEnv();
});

test("renderStreetCoverage: age mode still works on a streetwalk artifact via the alias", async () => {
  // The manifest path must not break the other view modes: styleForMode("age")
  // reads nearest_pano_age_years, which normalize aliased from the median.
  const env = stubRenderEnv(() => streetwalkArtifact());
  await renderStreetCoverage(env.map, GRID_RUN, "gsv", { streetwalkFile: WALK_FILE });
  assert.equal(
    styleForMode(env.fc.features[0], "age", "gsv").color,
    "color(3.5,gsv)" // median_covered_age_years, aliased
  );
  teardownRenderEnv();
});

test("renderStreetCoverage: tooltip shows the coverage percentage for a fractional edge", async () => {
  const env = stubRenderEnv(() => streetwalkArtifact());
  await renderStreetCoverage(env.map, GRID_RUN, "gsv", { streetwalkFile: WALK_FILE });
  const tips = [];
  env.geoJsonOpts.onEachFeature(env.fc.features[0], {
    bindTooltip: (text) => tips.push(text),
  });
  assert.equal(tips[0], "residential · covered 42% · 2022-06");
  teardownRenderEnv();
});

test("renderStreetCoverage: an uncovered edge's tooltip carries no percentage", async () => {
  const artifact = streetwalkArtifact();
  artifact.features[0].properties = { highway: "service", covered: false, coverage_fraction: 0 };
  const env = stubRenderEnv(() => artifact);
  await renderStreetCoverage(env.map, GRID_RUN, "gsv", { streetwalkFile: WALK_FILE });
  const tips = [];
  env.geoJsonOpts.onEachFeature(env.fc.features[0], { bindTooltip: (t) => tips.push(t) });
  assert.equal(tips[0], "service · no coverage");
  teardownRenderEnv();
});

test("renderStreetCoverage: a missing artifact is a silent no-op (no layer added)", async () => {
  const env = stubRenderEnv(() => {
    throw new Error("404");
  });
  await assert.doesNotReject(
    renderStreetCoverage(env.map, GRID_RUN, "gsv", { streetwalkFile: WALK_FILE })
  );
  assert.equal(env.added, 0);
  teardownRenderEnv();
});

test("renderStreetCoverage: an artifact with no features or no metadata block adds nothing", async () => {
  let env = stubRenderEnv(() => ({ type: "FeatureCollection", features: [] }));
  await renderStreetCoverage(env.map, GRID_RUN, "gsv", { streetwalkFile: WALK_FILE });
  assert.equal(env.added, 0);
  teardownRenderEnv();

  // Present features but a truncated/partially-uploaded metadata block: the
  // panel is driven entirely by it, so the whole overlay bails rather than throw.
  const noMeta = streetwalkArtifact();
  delete noMeta.properties.metadata.coverage_by_highway;
  env = stubRenderEnv(() => noMeta);
  await assert.doesNotReject(
    renderStreetCoverage(env.map, GRID_RUN, "gsv", { streetwalkFile: WALK_FILE })
  );
  assert.equal(env.added, 0);
  teardownRenderEnv();
});

// ── Issue #104: the legend section, the layer control and the controller ────

/** A StreetUiState with sensible defaults; override per test. */
function uiState(overrides = {}) {
  return {
    mode: "coverage",
    gapsOnly: false,
    hasFractional: true,
    provider: "gsv",
    providerLabel: "Google Street View",
    networkLabel: null,
    coveredPct: 85.1,
    covered: 2,
    segments: 2,
    ...overrides,
  };
}

/** Every `<i ...>` opening tag in an HTML string. */
const chipTags = (html) => html.match(/<i\b[^>]*>/g) || [];

test("streetLegendChipsHtml: coverage mode on a fractional artifact shows the three-stop ramp and a dashed slate gap chip", () => {
  const html = streetLegendChipsHtml(uiState());
  const stops = [STREET_PARTIAL_LOW_COLOR, STREET_PARTIAL_MID_COLOR, STREET_COVERED_COLOR].join(",");
  assert.equal(stops, "#e2f5ea,#7ccf9f,#2fb974");
  assert.ok(html.includes(`linear-gradient(90deg,${stops})`), html);
  assert.ok(html.includes("partial → full"));
  assert.ok(/<i class="dashed" style="color:#9aa3ad"/.test(html), html);
  // The swatches are decorative: the chip text carries the meaning.
  const tags = chipTags(html);
  assert.equal(tags.length, 2);
  for (const tag of tags) assert.ok(tag.includes('aria-hidden="true"'), tag);
});

test("streetLegendChipsHtml: coverage mode on a binary artifact shows a solid covered chip, not the ramp", () => {
  const html = streetLegendChipsHtml(uiState({ hasFractional: false }));
  assert.ok(html.includes(`background:${STREET_COVERED_COLOR}`), html);
  assert.ok(!html.includes("linear-gradient"), html);
  assert.ok(html.includes(">covered<"), html);
});

test("streetLegendChipsHtml: gapsOnly flips the gap chip to the highlight red", () => {
  const on = streetLegendChipsHtml(uiState({ gapsOnly: true }));
  const off = streetLegendChipsHtml(uiState({ gapsOnly: false }));
  assert.ok(on.includes(`color:${STREET_GAP_HIGHLIGHT_COLOR}`), on);
  assert.ok(!on.includes(`color:${STREET_UNCOVERED_COLOR}`), on);
  assert.ok(off.includes(`color:${STREET_UNCOVERED_COLOR}`), off);
});

test("streetLegendChipsHtml: type mode shows only the gap chip (the chart is the type legend)", () => {
  const html = streetLegendChipsHtml(uiState({ mode: "type" }));
  assert.equal(chipTags(html).length, 1);
  assert.ok(html.includes("no coverage"));
});

test("streetLegendChipsHtml: age mode threads the PROVIDER into the ramp", () => {
  const html = streetLegendChipsHtml(uiState({ mode: "age", provider: "mapillary" }));
  assert.ok(
    html.includes(
      "linear-gradient(90deg,color(0,mapillary),color(3,mapillary),color(6,mapillary),color(10,mapillary))"
    ),
    html
  );
  assert.ok(html.includes("newer → older"));
});

test("streetLegendSectionHtml: exactly one radio is checked and it is the state's mode", () => {
  for (const mode of ["age", "coverage", "type"]) {
    const html = streetLegendSectionHtml(uiState({ mode }));
    assert.ok(html.includes('role="radiogroup"'));
    assert.equal((html.match(/role="radio"/g) || []).length, 3);
    const checked = html.match(/<button[^>]*aria-checked="true"[^>]*>/g) || [];
    assert.equal(checked.length, 1, `${mode}: ${checked}`);
    assert.ok(checked[0].includes(`data-mode="${mode}"`), checked[0]);
    assert.ok(checked[0].includes("active"), checked[0]);
    // .gsv-mode-toggle means "this run has the Google-only filter" (the e2e
    // suite asserts its ABSENCE on Panoramax/KartaView runs, which have walks).
    assert.ok(!html.includes("gsv-mode-toggle"), html);
  }
});

test("streetLegendSectionHtml: the gaps checkbox mirrors state and the headline carries pct, counts and the ESCAPED provider label", () => {
  const off = streetLegendSectionHtml(uiState({ providerLabel: "<b>X</b>" }));
  assert.ok(off.includes("&lt;b&gt;X&lt;/b&gt;"), off);
  assert.ok(!off.includes("<b>X</b>"), off);
  assert.ok(off.includes("85.1%"));
  assert.ok(/2 of\s+2 segments covered/.test(off), off);
  const box = (html) => html.match(/<input[^>]*id="street-gaps-toggle"[^>]*>/)[0];
  assert.ok(!/\schecked[\s>]/.test(box(off)), box(off));
  const on = streetLegendSectionHtml(uiState({ gapsOnly: true }));
  assert.ok(/\schecked[\s>]/.test(box(on)), box(on));
});

test("streetLegendSectionHtml: names the network only when given one", () => {
  const header = (html) => html.match(/<div class="legend-year-header">([^<]*)<\/div>/)[1];
  assert.equal(
    header(streetLegendSectionHtml(uiState({ networkLabel: "Roads + paths" }))),
    "Street coverage · Roads + paths"
  );
  assert.equal(header(streetLegendSectionHtml(uiState({ networkLabel: null }))), "Street coverage");
});

test("panoVisibilityLayer: add → visible, remove → hidden", () => {
  stubRenderEnv(() => null);
  const seen = [];
  const proxy = panoVisibilityLayer((v) => seen.push(v));
  proxy.handlers.add();
  proxy.handlers.remove();
  assert.deepEqual(seen, [true, false]);
  teardownRenderEnv();
});

test("renderStreetCoverage: builds an expanded top-left layer control with Panoramas then Streets", async () => {
  const env = stubRenderEnv(() => streetwalkArtifact());
  const panoLayer = { pano: true };
  await renderStreetCoverage(env.map, GRID_RUN, "gsv", { streetwalkFile: WALK_FILE, panoLayer });
  const { base, overlays, opts } = env.layersControl;
  assert.equal(base, null);
  assert.deepEqual(Object.keys(overlays), ["Panoramas", "Streets"]);
  assert.equal(overlays.Panoramas, panoLayer);
  assert.equal(overlays.Streets, env.layer);
  assert.deepEqual(opts, { position: "topleft", collapsed: false });
  assert.equal(env.controlEl.attrs["aria-label"], "Map layers");
  assert.equal(env.controlEl.attrs.role, "group");
  teardownRenderEnv();
});

test("renderStreetCoverage: without a pano layer the control lists Streets alone", async () => {
  const env = stubRenderEnv(() => gridArtifact());
  await renderStreetCoverage(env.map, GRID_RUN, "gsv", {});
  assert.deepEqual(Object.keys(env.layersControl.overlays), ["Streets"]);
  teardownRenderEnv();
});

test("renderStreetCoverage: a missing artifact builds no layer control and resolves null", async () => {
  const env = stubRenderEnv(() => {
    throw new Error("404");
  });
  const changes = [];
  const result = await renderStreetCoverage(env.map, GRID_RUN, "gsv", {
    streetwalkFile: WALK_FILE,
    panoLayer: {},
    onChange: (s) => changes.push(s),
  });
  assert.equal(result, null);
  assert.equal(env.layersControl, undefined);
  assert.equal(env.added, 0);
  assert.deepEqual(changes, []);
  teardownRenderEnv();
});

test("renderStreetCoverage: emits the initial state once the overlay exists", async () => {
  let env = stubRenderEnv(() => streetwalkArtifact());
  let changes = [];
  await renderStreetCoverage(env.map, GRID_RUN, "gsv", {
    streetwalkFile: WALK_FILE,
    networkType: "all_public",
    onChange: (s) => changes.push(s),
  });
  assert.equal(changes.length, 1);
  assert.equal(changes[0].mode, "coverage");
  assert.equal(changes[0].hasFractional, true);
  assert.equal(changes[0].networkLabel, "Roads + paths");
  assert.equal(changes[0].providerLabel, "Google Street View");
  assert.equal(changes[0].coveredPct, 98.4);
  assert.equal(changes[0].covered, 2); // aliased from edges_any_coverage
  assert.equal(changes[0].segments, 2); // aliased from edges
  teardownRenderEnv();

  // The derived grid artifact declares no network, so none is named even
  // when the caller passes one.
  env = stubRenderEnv(() => gridArtifact());
  changes = [];
  await renderStreetCoverage(env.map, GRID_RUN, "gsv", {
    networkType: "all_public",
    onChange: (s) => changes.push(s),
  });
  assert.equal(changes.length, 1);
  assert.equal(changes[0].mode, "age");
  assert.equal(changes[0].networkLabel, null);
  teardownRenderEnv();
});

test("renderStreetCoverage: resolves a controller whose setMode restyles the layer and reports the new state", async () => {
  const env = stubRenderEnv(() => streetwalkArtifact());
  const changes = [];
  const ctl = await renderStreetCoverage(env.map, GRID_RUN, "gsv", {
    streetwalkFile: WALK_FILE,
    onChange: (s) => changes.push(s),
  });
  ctl.setMode("type");
  assert.equal(changes.length, 2);
  assert.equal(changes.at(-1).mode, "type");
  assert.equal(ctl.getState().mode, "type");
  const feature = { properties: { highway: "residential", covered: true } };
  assert.equal(env.styleFn(feature).color, streetTypeColor("residential"));
  // The reported state is a copy: mutating it cannot reach into the controller.
  changes.at(-1).mode = "age";
  assert.equal(ctl.getState().mode, "type");
  teardownRenderEnv();
});

test("renderStreetCoverage: setGaps(true) paints an uncovered edge red and reports gapsOnly", async () => {
  const env = stubRenderEnv(() => streetwalkArtifact());
  const changes = [];
  const ctl = await renderStreetCoverage(env.map, GRID_RUN, "gsv", {
    streetwalkFile: WALK_FILE,
    onChange: (s) => changes.push(s),
  });
  ctl.setGaps(true);
  const gap = { properties: { covered: false, highway: "service" } };
  assert.equal(env.styleFn(gap).color, STREET_GAP_HIGHLIGHT_COLOR);
  assert.equal(changes.at(-1).gapsOnly, true);
  ctl.setGaps(false);
  assert.equal(env.styleFn(gap).color, STREET_UNCOVERED_COLOR);
  assert.equal(changes.at(-1).gapsOnly, false);
  teardownRenderEnv();
});

test("renderStreetCoverage: setMode with an unknown or unchanged mode is a no-op", async () => {
  const env = stubRenderEnv(() => streetwalkArtifact());
  const changes = [];
  const ctl = await renderStreetCoverage(env.map, GRID_RUN, "gsv", {
    streetwalkFile: WALK_FILE,
    onChange: (s) => changes.push(s),
  });
  ctl.setMode("bogus");
  ctl.setMode("coverage"); // already the mode
  assert.equal(changes.length, 1); // only the initial emit
  assert.equal(env.styleFn, null); // never restyled
  assert.equal(ctl.getState().mode, "coverage");
  teardownRenderEnv();
});

test("city.html: the street panel is gone and the chart panel starts hidden above the temporal plot", () => {
  const html = require("node:fs").readFileSync(require("node:path").join(__dirname, "../../city.html"), "utf8");
  assert.ok(!html.includes("street-coverage-container"));
  const panel = html.match(/<div id="street-chart-container"[^>]*>/);
  assert.ok(panel, "chart panel missing");
  assert.ok(/\shidden[\s>]/.test(panel[0]), panel[0]);
  const stack = html.indexOf('id="bottom-right-panels"');
  const chart = html.indexOf('id="street-chart-container"');
  const temporal = html.indexOf('id="temporal-plot-container"');
  assert.ok(stack !== -1 && stack < chart && chart < temporal, { stack, chart, temporal });
  // Both panels sit inside the stack: nothing closes it before the temporal one.
  const stackBody = html.slice(stack, temporal);
  const opens = (stackBody.match(/<div\b/g) || []).length;
  const closes = (stackBody.match(/<\/div>/g) || []).length;
  assert.ok(opens - closes >= 1, "temporal panel is outside #bottom-right-panels");
});

// --- gap highlight ("Highlight gaps" toggle) --------------------------------

test("styleWithGapHighlight: uncovered flips to the red gap color, dashed, full opacity", () => {
  const base = styleStreetByType({ properties: { highway: "residential", covered: false } });
  const s = styleWithGapHighlight(base, false);
  assert.equal(s.color, STREET_GAP_HIGHLIGHT_COLOR);
  assert.equal(s.opacity, 1);
  assert.equal(s.dashArray, "4 4");
  assert.equal(s.weight, 3);
});

test("styleWithGapHighlight: covered keeps its base style but fades to a whisper", () => {
  const base = styleStreetByCoverage({ properties: { covered: true, coverage_fraction: 0.8 } });
  const s = styleWithGapHighlight(base, true);
  assert.equal(s.color, base.color); // hue preserved — only emphasis changes
  assert.equal(s.opacity, 0.15);
  assert.equal(s.dashArray, undefined);
});

// --- orderedStreetTypes (chart row order = legend order = artifact order) ----

test("orderedStreetTypes: canonical hierarchy, not length order", () => {
  const byType = {
    residential: { length_km: 100 },
    motorway: { length_km: 1 },
    footway: { length_km: 500 },
    alley: { length_km: 2 },
  };
  assert.deepEqual(orderedStreetTypes(byType), [
    "motorway",
    "residential",
    "alley",
    "footway",
  ]);
});

test("orderedStreetTypes: unknown buckets sort last", () => {
  assert.deepEqual(orderedStreetTypes({ zzz_mystery: {}, trunk: {} }), ["trunk", "zzz_mystery"]);
});

// --- chart bar colors + in-bar label ink ------------------------------------

test("chartBarBaseColor: covered wears the row's type hue, uncovered the gap slate", () => {
  assert.equal(chartBarBaseColor(0, "residential"), STREET_TYPE_COLORS.residential);
  assert.equal(chartBarBaseColor(0, "alley"), STREET_TYPE_COLORS.service); // family hue
  assert.equal(chartBarBaseColor(1, "residential"), STREET_UNCOVERED_COLOR);
});

test("labelTextColorFor: dark ink on light fills, white on dark fills", () => {
  assert.equal(labelTextColorFor("#e2f5ea"), "#0e1a12"); // pale green
  assert.equal(labelTextColorFor(STREET_TYPE_COLORS.residential), "#0e1a12"); // coral, light
  assert.equal(labelTextColorFor(STREET_TYPE_COLORS.secondary), "#fff"); // dark green
  // Medium hues sit near the boundary — motorway blue takes dark ink, which
  // is its higher-contrast pairing (≈4.6:1 vs ≈3.7:1 for white).
  assert.equal(labelTextColorFor(STREET_TYPE_COLORS.motorway), "#0e1a12");
  assert.equal(labelTextColorFor("#000000"), "#fff");
});
