// Offline unit tests for the pure helpers in street-coverage.js (issue #24).
// Run with `npm test` (Node's built-in test runner) — no network, no jsdom.
// In the browser these helpers read shared globals from streetscape-utils.js;
// here we stub just the two they touch (STREETSCAPE_DATA_BASE_URL, getColor).

const test = require("node:test");
const assert = require("node:assert/strict");

global.STREETSCAPE_DATA_BASE_URL = "https://example.test/data/";
global.getColor = (age, provider) => `color(${age},${provider})`;

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
  STREET_UNCOVERED_COLOR,
  STREET_COVERED_COLOR,
  STREET_COVERED_NODATE_COLOR,
  STREET_TYPE_MINOR_COLOR,
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
  // Endpoints are the ramp anchors exactly.
  assert.equal(fractionColor(0), "rgb(191, 232, 212)"); // STREET_PARTIAL_LOW_COLOR #bfe8d4
  assert.equal(fractionColor(1), "rgb(47, 185, 116)"); // STREET_COVERED_COLOR #2fb974
  // Green channel decreases as fraction rises (232 → 185): a simple monotonicity check.
  const g = (c) => Number(c.match(/rgb\(\d+, (\d+),/)[1]);
  assert.ok(g(fractionColor(0)) > g(fractionColor(0.5)));
  assert.ok(g(fractionColor(0.5)) > g(fractionColor(1)));
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
        totals: { edges: 41, edges_any_coverage: 40, uncovered_pct_by_length: 4.4 },
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
// The panel is skipped in these tests (buildStreetCoveragePanel early-returns
// when #street-coverage-container is absent), so they exercise exactly the
// fetch/normalize/style seam without needing a DOM or Chart.js.

/** Minimal Leaflet + DOM stubs; returns a handle on what the renderer built. */
function stubRenderEnv(fetchImpl) {
  const captured = { urls: [], geoJsonOpts: null, added: 0 };
  global.fetchGzippedJson = async (url) => {
    captured.urls.push(url);
    return fetchImpl(url);
  };
  global.document = { getElementById: () => null };
  global.L = {
    geoJSON: (fc, opts) => {
      captured.geoJsonOpts = opts;
      captured.fc = fc;
      return {
        addTo: () => {
          captured.added += 1;
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
        totals: { edges: 2, edges_any_coverage: 2, uncovered_pct_by_length: 1.6 },
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
        totals: { segments: 2, covered: 1, uncovered_pct_by_length: 12.0 },
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
