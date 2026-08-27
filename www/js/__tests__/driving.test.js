// Offline unit tests for the pure helpers in driving.js — the Driving page,
// which joins Google's published plan against observed imagery. Run with
// `npm test` (Node's built-in test runner) — no network, no jsdom. `document`
// is left undefined on purpose: driving.js only registers its DOMContentLoaded
// listener when one exists.

const test = require("node:test");
const assert = require("node:assert/strict");

// The streetwalk manifest helper lives in streetscape-utils.js; the page reads
// it as a browser global.
global.lookupStreetwalk = (manifest, cityId, provider, networkType) =>
  (manifest?.walks ?? []).find(
    (w) => w.city_id === cityId && w.provider === provider && w.network_type === networkType
  ) ?? null;

// Mirrors streetscape-utils.js exactly, quotes included. A stub that escaped
// only &<> would pass every assertion here while leaving the real risk — the
// feed-derived strings this page interpolates into title="…" attributes —
// untested, so a regression in attribute escaping would look green.
global.escapeHtml = (value) => {
  if (value == null) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
};
global.coverageColor = (pct) => `coverage(${pct})`;
// Local-midnight parse, matching streetscape-utils.js — a bare YYYY-MM-DD read
// as UTC can shift a date by a day, which would show up as an off-by-one in
// the days-left column.
global.panoDateOrNull = (v) => {
  if (!v) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(v));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
};

// driving.js reads the shared table machinery as browser globals — mirror that
// here (must precede the driving.js require).
Object.assign(global, require("../table-utils.js"));

const {
  drivingRowModel,
  planAreaRowModel,
  buildPlaceRows,
  mergeStreetCoverage,
  windowRangeCellHtml,
  drivingRowHtml,
  daysUntil,
  sparklineCellHtml,
  revisionItemHtml,
  VERDICTS,
  DRIVING_COLUMNS,
  DRIVING_PRESETS,
  DRIVING_FILTERS,
  DRIVING_SEARCH_FIELDS,
  DRIVING_DEFAULT_SORT,
} = require("../driving.js");

const TODAY = new Date(2026, 7, 16); // 2026-08-16, local

// Tel-Aviv is the case the page exists for: Google's feed says the campaign
// closed in Feb 2019, our own run recorded a capture in Oct 2023.
const TEL_AVIV = {
  city_id: "tel-aviv--tel-aviv-district--israel",
  display_name: "Tel-Aviv, Tel-Aviv District, Israel",
  city_name: "Tel-Aviv",
  state_name: "Tel-Aviv District",
  country_name: "Israel",
  enabled: true,
  verdict: "driven_unplanned",
  plan: {
    match_tier: "manual",
    entry_count: 1,
    active_count: 0,
    window_start: "2019-02-14",
    window_end: "2019-02-28",
    window_approximate: true,
    districts: ["תל אביב"],
  },
  observed: {
    gsv: {
      run_date: "2026-08-12",
      csv_filename: "tel-aviv_width_1_height_1_step_20_2026-08-12.csv.gz",
      coverage_rate_pct: 57.9,
      newest_capture: "2023-10-01",
      years_since_newest_capture: 2.87,
      google_panos: 101975,
      change: { from: "2026-02-01", capture_date_changed: 412 },
    },
    mapillary: {
      run_date: "2026-08-12",
      csv_filename: "tel-aviv_width_1_height_1_step_20_mapillary_2026-08-12.csv.gz",
      coverage_rate_pct: 1.2,
    },
  },
};

// Addis Ababa: absent from the feed entirely, so no `plan` key at all.
const ADDIS = {
  city_id: "addis-ababa--addis-ababa--ethiopia",
  display_name: "Addis Ababa, Addis Ababa, Ethiopia",
  city_name: "Addis Ababa",
  state_name: "Addis Ababa",
  country_name: "Ethiopia",
  enabled: true,
  verdict: "not_listed",
  observed: {
    gsv: {
      run_date: "2026-08-12",
      csv_filename: "addis_width_1_height_1_step_20_2026-08-12.csv.gz",
      coverage_rate_pct: 2.7,
      newest_capture: "2026-07-01",
      google_panos: 0,
    },
  },
};

test("drivingRowModel: flattens the plan and observed blocks", () => {
  const row = drivingRowModel(TEL_AVIV, TODAY);
  assert.equal(row.cityId, TEL_AVIV.city_id);
  assert.equal(row.verdict, "driven_unplanned");
  assert.equal(row.planStatus, "Closed");
  assert.equal(row.windowEnd, "2019-02-28");
  assert.equal(row.windowApproximate, true);
  assert.equal(row.coveragePct, 57.9);
  assert.equal(row.googlePanos, 101975);
  assert.equal(row.newestCapture, "2023-10-01");
  assert.equal(row.captureDateChanged, 412);
  assert.equal(row.mapillaryPct, 1.2);
  assert.equal(row.matchTier, "manual");
});

test("drivingRowModel: an absent plan block yields nulls, never undefined", () => {
  // `plan` is absent-not-null in the artifact, so every plan-derived field has
  // to survive the key simply not being there — a sortable column reading
  // `undefined` would sort inconsistently against nulls.
  const row = drivingRowModel(ADDIS, TODAY);
  assert.equal(row.planStatus, null);
  assert.equal(row.windowStart, null);
  assert.equal(row.windowEnd, null);
  assert.equal(row.daysToWindowEnd, null);
  assert.equal(row.matchTier, null);
  assert.equal(row.windowApproximate, false);
  assert.equal(row.verdict, "not_listed");
});

test("drivingRowModel: a city with no runs at all still yields a row", () => {
  const row = drivingRowModel(
    { city_id: "x", display_name: "X", verdict: "not_listed", enabled: false },
    TODAY
  );
  assert.equal(row.coveragePct, null);
  assert.equal(row.newestCapture, null);
  assert.equal(row.filename, null);
  assert.equal(row.enabled, false);
});

test("drivingRowModel: prefers the GSV run for the city-page link", () => {
  // The page is about Google's driving, so the GSV snapshot is the one a
  // reader wants opened — even though a Mapillary run exists for the same city.
  const row = drivingRowModel(TEL_AVIV, TODAY);
  assert.match(row.filename, /2026-08-12\.csv\.gz$/);
  assert.doesNotMatch(row.filename, /mapillary/);
});

test("drivingRowModel: falls back to the Mapillary run when there is no GSV one", () => {
  const mapillaryOnly = {
    city_id: "x",
    display_name: "X",
    verdict: "not_listed",
    observed: { mapillary: { run_date: "2026-01-01", csv_filename: "x_mapillary.csv.gz" } },
  };
  assert.equal(drivingRowModel(mapillaryOnly, TODAY).filename, "x_mapillary.csv.gz");
});

test("drivingRowModel: districts are joined so search can find a city by county", () => {
  // US plan entries are keyed by county, so "Ada" has to find Boise even
  // though no column displays the district.
  const boise = {
    city_id: "boise--idaho--united-states",
    display_name: "Boise, Idaho, United States",
    verdict: "drive_confirmed",
    plan: { match_tier: "region", active_count: 47, districts: ["Ada", "Adams", "Boise"] },
  };
  const row = drivingRowModel(boise, TODAY);
  assert.equal(row.districts, "Ada Adams Boise");
  assert.ok(DRIVING_SEARCH_FIELDS.includes("districts"));
});

test("daysUntil: counts whole days, negative once the date has passed", () => {
  assert.equal(daysUntil("2026-08-16", TODAY), 0);
  assert.equal(daysUntil("2026-08-20", TODAY), 4);
  assert.equal(daysUntil("2026-08-06", TODAY), -10);
  assert.equal(daysUntil(null, TODAY), null);
});

test("daysUntil: an already-closed window reads negative, not absent", () => {
  // "Closed 7 years ago" and "no window at all" are different facts and must
  // not collapse into the same cell — that distinction is the whole Israel
  // finding.
  const row = drivingRowModel(TEL_AVIV, TODAY);
  assert.ok(row.daysToWindowEnd < 0);
  assert.equal(drivingRowModel(ADDIS, TODAY).daysToWindowEnd, null);
});

test("every sortable column key exists on a row model", () => {
  // Guards the column/model seam: a sortable column with no matching model
  // field would silently sort every row as null.
  const row = drivingRowModel(TEL_AVIV, TODAY);
  for (const col of DRIVING_COLUMNS.filter((c) => c.sortable !== false)) {
    assert.ok(col.key in row, `row model is missing ${col.key}`);
  }
  assert.ok(DRIVING_COLUMNS.some((c) => c.key === DRIVING_DEFAULT_SORT.key));
});

test("every column renders a cell, including from a fully sparse row model", () => {
  // The header and body both come from DRIVING_COLUMNS, so a column that
  // throws on a sparse city takes down the whole table rather than rendering
  // one bad cell.
  const sparse = drivingRowModel({ city_id: "x" }, TODAY);
  for (const col of DRIVING_COLUMNS) {
    assert.equal(typeof col.cell, "function", `${col.key} has no cell renderer`);
    assert.match(col.cell(sparse), /^<t[hd][\s>]/, `${col.key} did not render a cell`);
  }
});

test("every preset names only real columns", () => {
  const keys = new Set(DRIVING_COLUMNS.map((c) => c.key));
  for (const preset of DRIVING_PRESETS) {
    for (const key of preset.columns) {
      assert.ok(keys.has(key), `preset ${preset.id} names unknown column ${key}`);
    }
  }
});

test("a row renders exactly one cell per visible column", () => {
  const html = drivingRowHtml(drivingRowModel(TEL_AVIV, TODAY));
  const cells = html.match(/<t[hd][\s>]/g) ?? [];
  assert.equal(cells.length, DRIVING_COLUMNS.length);
});

test("city names are HTML-escaped (OSM data is publicly editable)", () => {
  const html = drivingRowHtml(
    drivingRowModel({ ...TEL_AVIV, display_name: "<script>alert(1)</script>" }, TODAY)
  );
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});

test("every verdict in the vocabulary renders a pill with its hint", () => {
  for (const [verdict, spec] of Object.entries(VERDICTS)) {
    const html = drivingRowHtml(drivingRowModel({ city_id: "x", verdict }, TODAY));
    assert.match(html, /verdict-pill/, `${verdict} did not render a pill`);
    assert.ok(html.includes(spec.label), `${verdict} did not render its label`);
  }
});

test("an unknown verdict degrades to plain text instead of throwing", () => {
  // The artifact could gain a verdict before the page learns about it; a
  // stale cached driving.js must still render the table.
  const html = drivingRowHtml(drivingRowModel({ city_id: "x", verdict: "brand_new" }, TODAY));
  assert.match(html, /brand_new/);
  assert.doesNotMatch(html, /verdict-pill/);
});

test("a closed campaign's hint says absence is not evidence of no driving", () => {
  // The single most important caveat on the page — if this text goes away the
  // table starts implying something false.
  assert.match(VERDICTS.closed.hint, /NOT evidence/i);
  assert.match(VERDICTS.not_listed.hint, /not a guarantee/i);
});

test("an approximate window is marked, so a heuristic never reads as fact", () => {
  const col = DRIVING_COLUMNS.find((c) => c.key === "windowEnd");
  const approx = col.cell(drivingRowModel(TEL_AVIV, TODAY));
  assert.match(approx, /approximate/);
  const clean = col.cell(
    drivingRowModel(
      { city_id: "x", verdict: "closed", plan: { match_tier: "region", active_count: 0, window_end: "2026-01-01" } },
      TODAY
    )
  );
  assert.doesNotMatch(clean, /approximate/);
});

// ── Plan-area rows: the places we do NOT track ────────────────────────────

const CHUBUT = {
  record_id: "plan:abc123def456",
  country: "Argentina",
  country_matched: "Argentina",
  region: "Chubut",
  publish: "Yes",
  window_start: "2026-01-01",
  window_end: "2026-12-31",
  districts: ["Esquel", "Rawson", "Trelew"],
  district_count: 3,
  matched_city_ids: [],
  matched_city_count: 0,
  verdict: "planned_open",
};

test("planAreaRowModel: produces the same shape a city row does", () => {
  // The two kinds share one table, so a missing key would sort inconsistently
  // against the city rows rather than failing loudly.
  const area = planAreaRowModel(CHUBUT, TODAY);
  const city = drivingRowModel(TEL_AVIV, TODAY);
  assert.deepEqual(Object.keys(area).sort(), Object.keys(city).sort());
});

test("plan status distinguishes an elapsed window from a live one", () => {
  // 214 production records are still flagged publish=Yes with a window that
  // closed months ago. Calling those "Active" put the column in direct
  // contradiction with the row's own "Campaign closed" verdict.
  const live = planAreaRowModel({ ...CHUBUT, window_end: "2026-12-31" }, TODAY);
  const elapsed = planAreaRowModel({ ...CHUBUT, window_end: "2025-10-01" }, TODAY);
  const closed = planAreaRowModel({ ...CHUBUT, publish: "No" }, TODAY);

  assert.equal(live.planStatus, "Active");
  assert.equal(elapsed.planStatus, "Elapsed");
  assert.equal(closed.planStatus, "Closed");
});

test("plan status distinguishes a window that has not opened yet", () => {
  // The symmetric case to Elapsed, and it exists for the same reason: a
  // published window starting next year is not "Active" either. Reporting it
  // as Active put "Plan status: Active" beside the verdict "Planned", under a
  // tooltip promising Active meant the window was open — and made the
  // "Window open now" filter select campaigns that had not started.
  const upcoming = planAreaRowModel(
    { ...CHUBUT, window_start: "2027-01-01", window_end: "2027-12-31" },
    TODAY
  );
  assert.equal(upcoming.planStatus, "Upcoming");

  // A window that opened today is Active, not Upcoming — the boundary is
  // inclusive, matching plan_match.classify's `planned_open`.
  const opensToday = planAreaRowModel(
    { ...CHUBUT, window_start: "2026-08-16", window_end: "2026-12-31" },
    TODAY
  );
  assert.equal(opensToday.planStatus, "Active");

  // And with no start date at all there is nothing to be upcoming about: a
  // published, unelapsed window stays Active rather than silently changing
  // state on the ~half of feed records that carry only one dirty date.
  const noStart = planAreaRowModel(
    { ...CHUBUT, window_start: null, window_end: "2026-12-31" },
    TODAY
  );
  assert.equal(noStart.planStatus, "Active");
});

test("plan status agrees with the verdict on an elapsed window", () => {
  // The two are computed independently (verdict in Python, status in JS), so
  // this pins the case where they used to disagree on screen.
  const row = planAreaRowModel(
    { ...CHUBUT, window_end: "2025-10-01", verdict: "closed" },
    TODAY
  );
  assert.equal(row.planStatus, "Elapsed");
  assert.equal(VERDICTS[row.verdict].label, "Campaign closed");
});

test("a city row's plan status uses the same states", () => {
  const elapsed = drivingRowModel(
    {
      city_id: "x",
      verdict: "closed",
      plan: { match_tier: "region", active_count: 3, window_end: "2025-01-01" },
    },
    TODAY
  );
  assert.equal(elapsed.planStatus, "Elapsed");
});

test("planAreaRowModel: every observed field is null, and it is not tracked", () => {
  const row = planAreaRowModel(CHUBUT, TODAY);
  assert.equal(row.scope, "area");
  assert.equal(row.enabled, false);
  assert.equal(row.coveragePct, null);
  assert.equal(row.googlePanos, null);
  assert.equal(row.newestCapture, null);
  assert.equal(row.captureYears, null);
  assert.equal(row.filename, null);
  // But the plan side is fully populated — that is the point of the row.
  assert.equal(row.planStatus, "Active");
  assert.equal(row.windowEnd, "2026-12-31");
  assert.equal(row.verdict, "planned_open");
  assert.equal(row.districts, "Esquel Rawson Trelew");
});

test("every plan-status filter value is one the row models can actually produce", () => {
  // A filter option nothing matches silently yields an empty table.
  const produced = new Set(
    [
      planAreaRowModel({ ...CHUBUT, window_end: "2026-12-31" }, TODAY),
      planAreaRowModel({ ...CHUBUT, window_end: "2025-10-01" }, TODAY),
      planAreaRowModel({ ...CHUBUT, publish: "No" }, TODAY),
      // Published, but the window has not opened yet — "Active" would put the
      // column in contradiction with the row's own "Planned" verdict, and make
      // the window-open filter select campaigns that have not started.
      planAreaRowModel(
        { ...CHUBUT, window_start: "2027-01-01", window_end: "2027-12-31" },
        TODAY
      ),
      drivingRowModel(ADDIS, TODAY),
    ].map((r) => r.planStatus ?? "None")
  );
  const filter = DRIVING_FILTERS.find((f) => f.key === "plan");
  for (const option of filter.options) {
    assert.ok(produced.has(option.value), `no row model produces plan status ${option.value}`);
  }
});

test("DRIVING_FILTERS: numeric filters are histogram sliders over real row fields", () => {
  // The same contract grid.js and streets.js carry. A `range` descriptor still
  // renders (two number inputs, no picture), so a filter that silently stayed
  // plain would look deliberate rather than broken — and on THIS page the bars
  // are load-bearing: most rows are plan areas we do not track, whose observed
  // fields are all null, so the histogram is what says how few of the ~3,800
  // rows a coverage window can match at all.
  const row = drivingRowModel(ADDIS, TODAY);
  const area = planAreaRowModel(CHUBUT, TODAY);
  for (const filter of DRIVING_FILTERS) {
    if (!filter.field) continue;
    assert.equal(filter.type, "histogram-range", `${filter.key} is not a histogram filter`);
    assert.ok(filter.field in row, `filter ${filter.key} reads a missing field ${filter.field}`);
    // Both row KINDS have to carry the field, or a domain seeded from the full
    // row set would read `undefined` off every untracked area.
    assert.ok(filter.field in area, `filter ${filter.key} is absent from a plan-area row`);
  }
});

test("planAreaRowModel: names the row by the districts it covers", () => {
  // Google's feed carries TEN separate Accra records — different districts,
  // different windows. Labelling them all "Accra, Ghana" made ten distinct
  // campaigns look like one duplicated row.
  assert.equal(
    planAreaRowModel({ ...CHUBUT, districts: ["Esquel"], district_count: 1 }, TODAY).label,
    "Esquel, Chubut, Argentina"
  );
  assert.equal(
    planAreaRowModel(CHUBUT, TODAY).label,
    "Esquel +2, Chubut, Argentina"
  );
});

test("planAreaRowModel: does not repeat the region when the district IS the region", () => {
  // Ghana's feed has an "Accra" record whose only district is also "Accra";
  // "Accra, Accra, Ghana" would be noise.
  const row = planAreaRowModel(
    { ...CHUBUT, region: "Accra", country_matched: "Ghana", districts: ["Accra"], district_count: 1 },
    TODAY
  );
  assert.equal(row.label, "Accra, Ghana");
});

test("planAreaRowModel: falls back to region, then country, when districts are absent", () => {
  assert.equal(
    planAreaRowModel({ ...CHUBUT, districts: [], district_count: 0 }, TODAY).label,
    "Chubut, Argentina"
  );
  assert.equal(
    planAreaRowModel({ ...CHUBUT, region: null, districts: [], district_count: 0 }, TODAY).label,
    "Argentina"
  );
});

test("two records of the same region get distinguishable labels", () => {
  // The bug this guards: ten "Accra, Ghana" rows in a row.
  const a = planAreaRowModel({ ...CHUBUT, districts: ["Adentan Municipal"], district_count: 1 }, TODAY);
  const b = planAreaRowModel({ ...CHUBUT, districts: ["Ga East Municipal"], district_count: 1 }, TODAY);
  assert.notEqual(a.label, b.label);
});

test("buildPlaceRows: a record covering tracked cities is NOT also a row", () => {
  // Otherwise Idaho appears once as a plan area and again as Boise, and the
  // same place is double-counted in every total on the page.
  const payload = {
    cities: [TEL_AVIV, ADDIS],
    records: [CHUBUT, { ...CHUBUT, record_id: "plan:matched", region: "Idaho", matched_city_count: 4 }],
  };
  const rows = buildPlaceRows(payload, TODAY);
  assert.equal(rows.length, 3);
  assert.deepEqual(
    rows.map((r) => r.scope),
    ["city", "city", "area"]
  );
  assert.ok(!rows.some((r) => r.region === "Idaho"));
});

test("buildPlaceRows: tolerates an artifact with neither collection", () => {
  assert.deepEqual(buildPlaceRows({}, TODAY), []);
  assert.deepEqual(buildPlaceRows(null, TODAY), []);
});

test("every column renders a cell from a plan-area row too", () => {
  // The seam guard above covers city rows; area rows exercise the null side of
  // every observed column at once.
  const area = planAreaRowModel(CHUBUT, TODAY);
  for (const col of DRIVING_COLUMNS) {
    assert.match(col.cell(area), /^<t[hd][\s>]/, `${col.key} did not render for an area row`);
  }
});

test("an area row has no city-page link, since there is no run to open", () => {
  const html = drivingRowHtml(planAreaRowModel(CHUBUT, TODAY));
  assert.doesNotMatch(html, /city\.html\?file=/);
  assert.match(html, /Chubut/);
});

// ── Grid vs street coverage ───────────────────────────────────────────────

test("street coverage is joined from the manifest, drive network only", () => {
  // all_public walks a much larger street set, so its percentage has a
  // different denominator; mixing the two would silently change what the
  // column means between rows.
  const rows = [drivingRowModel(TEL_AVIV, TODAY), planAreaRowModel(CHUBUT, TODAY)];
  const manifest = {
    walks: [
      { city_id: TEL_AVIV.city_id, provider: "gsv", network_type: "drive", coverage_pct_by_length: 61.3 },
      { city_id: TEL_AVIV.city_id, provider: "gsv", network_type: "all_public", coverage_pct_by_length: 12.0 },
    ],
  };
  assert.equal(mergeStreetCoverage(rows, manifest), 1);
  assert.equal(rows[0].streetPct, 61.3);
  assert.equal(rows[1].streetPct, null, "a plan area has no city to walk");
});

test("an unwalked city keeps a null street coverage, never a zero", () => {
  // Street and grid coverage have different denominators, so the street column
  // is NOT a fallback — an unwalked city must read "no data", not 0%.
  const rows = [drivingRowModel(TEL_AVIV, TODAY)];
  mergeStreetCoverage(rows, { walks: [] });
  assert.equal(rows[0].streetPct, null);
  assert.notEqual(rows[0].coveragePct, null, "grid coverage is unaffected");
});

test("a missing manifest degrades the column instead of failing the page", () => {
  const rows = [drivingRowModel(TEL_AVIV, TODAY)];
  assert.equal(mergeStreetCoverage(rows, null), 0);
  assert.equal(rows[0].streetPct, null);
});

// ── Drive window ──────────────────────────────────────────────────────────

test("the drive window renders as a span, not a lone date", () => {
  const html = windowRangeCellHtml({ windowStart: "2026-03-01", windowEnd: "2026-11-01" });
  assert.match(html, /2026-03-01 → 2026-11-01/);
});

test("a single-day window is not rendered as an arrow to itself", () => {
  const html = windowRangeCellHtml({ windowStart: "2025-10-01", windowEnd: "2025-10-01" });
  assert.doesNotMatch(html, /→/);
  assert.match(html, /2025-10-01/);
});

test("a half-known window still renders, and an absent one is an em dash", () => {
  assert.match(windowRangeCellHtml({ windowStart: null, windowEnd: "2026-11-01" }), /\? → 2026-11-01/);
  assert.equal(windowRangeCellHtml({ windowStart: null, windowEnd: null }), "<td>—</td>");
});

test("an approximate window keeps its marker in the range cell", () => {
  const html = windowRangeCellHtml({
    windowStart: "2019-02-14",
    windowEnd: "2019-02-28",
    windowApproximate: true,
  });
  assert.match(html, /approximate/);
});

// ── Capture-history sparkline ─────────────────────────────────────────────

test("sparklineCellHtml: one bar per year, gaps kept as empty bars", () => {
  // A year with no imagery is a real observation — the gap between drives —
  // so it must keep its slot rather than closing up.
  const html = sparklineCellHtml({ captureYears: [2019, [10, 0, 0, 5]] });
  assert.equal((html.match(/spark-bar/g) ?? []).length, 4);
  assert.equal((html.match(/spark-empty/g) ?? []).length, 2);
  assert.match(html, /2019–2022/);
});

test("sparklineCellHtml: names the busiest year, which is the drive", () => {
  const html = sparklineCellHtml({ captureYears: [2018, [1, 1, 9000, 1]] });
  assert.match(html, /busiest 2020/);
});

test("sparklineCellHtml: a small year stays visible beside a dominant one", () => {
  // Real case: Kalamazoo has 64,871 panos from one 2026 drive and a few
  // hundred surviving from earlier passes. On a linear scale those earlier
  // drives render as invisible slivers, which defeats the column — so the
  // heights are log-scaled.
  const html = sparklineCellHtml({ captureYears: [2020, [200, 64871]] });
  const heights = [...html.matchAll(/height:(\d+)%/g)].map((m) => Number(m[1]));
  assert.equal(heights.length, 2);
  assert.equal(heights[1], 100, "the dominant year is still full height");
  assert.ok(heights[0] > 40, `a 200-pano year should stay legible, got ${heights[0]}%`);
});

test("sparklineCellHtml: a zero year is visibly different from a small one", () => {
  const html = sparklineCellHtml({ captureYears: [2020, [0, 1]] });
  assert.match(html, /spark-empty/);
  const heights = [...html.matchAll(/height:(\d+)%/g)].map((m) => Number(m[1]));
  assert.equal(heights[0], 0, "no imagery that year");
  assert.ok(heights[1] >= 12, "a single pano still gets a minimum visible bar");
});

test("sparklineCellHtml: bars are aria-hidden with a text equivalent", () => {
  const html = sparklineCellHtml({ captureYears: [2020, [3, 4]] });
  assert.match(html, /aria-hidden="true"/);
  assert.match(html, /visually-hidden/);
});

test("sparklineCellHtml: degrades to an em dash rather than throwing", () => {
  for (const row of [{}, { captureYears: null }, { captureYears: [] }, { captureYears: [2020, []] }]) {
    assert.equal(sparklineCellHtml(row), "<td>—</td>");
  }
});

test("captureSpanYears sorts the sparkline column and is null when absent", () => {
  const withYears = drivingRowModel({ ...TEL_AVIV, capture_years: [2015, [1, 0, 0, 2]] }, TODAY);
  assert.equal(withYears.captureSpanYears, 3);
  assert.equal(drivingRowModel(ADDIS, TODAY).captureSpanYears, null);
});

// ── Plan revisions ────────────────────────────────────────────────────────

test("revisionItemHtml: summarises counters and names example regions", () => {
  const html = revisionItemHtml({
    from: "2026-08-05",
    to: "2026-08-11",
    campaigns_closed: 6,
    campaigns_reopened: 0,
    windows_changed: 0,
    districts_changed: 1,
    regions_added: 0,
    regions_removed: 1,
    detail: {
      districts: [{ country: "Austria", region: "Steiermark", gained_count: 1, lost_count: 20 }],
      closed: [{ country: "Argentina", region: "Santa Fe" }],
      windows: [],
    },
  });
  assert.match(html, /2026-08-05 → 2026-08-11/);
  assert.match(html, /6 campaigns closed/);
  assert.match(html, /1 district list edited/);
  assert.match(html, /Steiermark/);
  assert.match(html, /−20 districts/);
});

test("revisionItemHtml: singular vs plural, and a revision that changed nothing regional", () => {
  const one = revisionItemHtml({
    from: "a", to: "b", campaigns_closed: 1, regions_added: 0, regions_removed: 0,
    campaigns_reopened: 0, windows_changed: 0, districts_changed: 0, detail: {},
  });
  assert.match(one, /1 campaign closed/);
  assert.doesNotMatch(one, /campaigns closed/);

  const none = revisionItemHtml({
    from: "a", to: "b", campaigns_closed: 0, regions_added: 0, regions_removed: 0,
    campaigns_reopened: 0, windows_changed: 0, districts_changed: 0, detail: {},
  });
  assert.match(none, /no regional changes/);
});

test("revisionItemHtml: survives an artifact with no detail block", () => {
  const html = revisionItemHtml({ from: "a", to: "b", campaigns_closed: 2 });
  assert.match(html, /2 campaigns closed/);
});
