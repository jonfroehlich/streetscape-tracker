// Offline unit tests for the pure helpers in streetscape-utils.js (issue #123).
// Run with `npm test` (Node's built-in test runner) — no network, no jsdom,
// no browser. These cover the numeric/date edge cases behind the B1–B4
// tooltip bugs (Infinity%/NaN) and the 0-pano epoch-date bug (#122/#69).

// Pin a west-of-UTC zone so the date-only-parse tests actually exercise the
// timezone-shift regression (CI runs in UTC, where UTC-parse bugs are
// invisible). Node honors TZ changes for subsequently created Dates.
process.env.TZ = "America/Los_Angeles";

const test = require("node:test");
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");

const {
  PROVIDERS,
  METRICS,
  adaptCityRecord,
  escapeHtml,
  getColor,
  coverageColor,
  getProviderFromFilename,
  isKnownProvider,
  isKnownMetric,
  parseFilterParam,
  isValidRunFilename,
  diffFilenameFor,
  isValidDiffFilename,
  recencyColor,
  FRESHNESS_BUCKETS,
  isGoogleCopyright,
  isPlausibleCaptureDate,
  panoDateOrNull,
  googleSharePercent,
  buildFilledHistogram,
  withAlpha,
  fmtYears,
  formatChangeSummary,
  RENDER_CAP,
  spatialStrideSample,
  computeVisibilityDelta,
  markerDateStyle,
  STREETSCAPE_DATA_BASE_URL,
  streetwalkManifestUrl,
  fetchStreetwalkManifest,
  lookupStreetwalk,
  mergeStreetwalkStats,
} = require("../streetscape-utils.js");

// --- adaptCityRecord: v1/v2/v3 aggregate flattening ------------------------

// A REAL v1 record, mirroring generate_aggregate_summary_as_json's output:
// flat fields, `city` is a bare string, data_file is an object, and NONE of
// the normalized keys (provider/pano_count/pano_age_stats/
// capture_year_histogram) exist. Regression: the adapter used to pass v1
// records through raw, so index.js crashed on
// pano_age_stats.median_pano_age_years with a live pre-v2 cities.json.gz.
const V1_RECORD = {
  city: "Bellingham",
  state: { name: "Washington", code: "WA" },
  country: { name: "United States", code: "us" },
  center: { latitude: 48.75, longitude: -122.48 },
  bounds: { northeast: {}, southwest: {} },
  data_file: {
    filename: "bellingham--wa_width_5000_height_5000_step_20.csv.gz",
    size_bytes: 12345,
  },
  search_area_km2: 25,
  coverage_rate_percent: 51.2,
  panorama_counts: { unique_panos: 100, unique_google_panos: 60 },
  all_panos_age_stats: { median_pano_age_years: 4.2 },
  google_panos_age_stats: { median_pano_age_years: 3.1 },
  collection_info: { start_time: "t0", end_time: "t1", duration_seconds: 60 },
  histogram_of_capture_dates_by_year: {
    all_panos: { 2019: 40, 2020: 60 },
    google_panos: { 2019: 25, 2020: 35 },
  },
};

test("adaptCityRecord: real v1 record gains the normalized keys", () => {
  const gsv = adaptCityRecord(V1_RECORD, "gsv");
  assert.equal(gsv.provider, "gsv");
  // Normalized keys derived from the flat v1 fields, preferring the
  // official-Google subset (same rule as v2/v3).
  assert.equal(gsv.pano_count, 60);
  assert.equal(gsv.pano_age_stats.median_pano_age_years, 3.1);
  assert.deepEqual(gsv.capture_year_histogram, { 2019: 25, 2020: 35 });
  // Fields the UI iterates/branches on must exist even though v1 lacks them.
  assert.deepEqual(gsv.runs, []);
  assert.equal(gsv.change, null);
  assert.equal(gsv.copyright_info_available, true);
  // Historical flat fields survive untouched (index.js reads
  // data_file.filename and the bare-string city name).
  assert.equal(gsv.city, "Bellingham");
  assert.equal(gsv.data_file.filename, V1_RECORD.data_file.filename);
  // v1 is gsv-only.
  assert.equal(adaptCityRecord(V1_RECORD, "mapillary"), null);
});

test("adaptCityRecord: v2 gsv-only providers-less record", () => {
  const v2 = {
    city_id: "x--wa",
    city: { name: "X", state: "WA", country: "USA" },
    latest: {
      run_date: "2026-01-01",
      panorama_counts: { unique_panos: 10, unique_google_panos: 7 },
      histogram_of_capture_dates_by_year: {},
      all_panos_age_stats: {},
      coverage_rate_percent: 1,
      search_area_km2: 1,
      data_file: "a",
      json_file: "b",
    },
    runs: [{ run_date: "2026-01-01" }],
    change: null,
  };
  const gsv = adaptCityRecord(v2, "gsv");
  assert.equal(gsv.provider, "gsv");
  assert.equal(gsv.city, "X");
  assert.equal(gsv.pano_count, 7); // unique_google_panos preferred for gsv
  assert.equal(gsv.latest_run_date, "2026-01-01");
  // v2 has no providers map, so non-gsv views are absent.
  assert.equal(adaptCityRecord(v2, "mapillary"), null);
});

test("adaptCityRecord: v3 per-provider block, null when provider missing", () => {
  const v3 = {
    city_id: "bend--or",
    city: { name: "Bend", state: "OR", country: "USA" },
    providers: {
      gsv: {
        latest: {
          run_date: "2026-07-05",
          panorama_counts: { unique_panos: 100, unique_google_panos: 60 },
          histogram_of_capture_dates_by_year: {
            google_panos: { counts: { 2020: 5 } },
            all_panos: { counts: { 2020: 8 } },
          },
          google_panos_age_stats: { median_pano_age_years: 3 },
          all_panos_age_stats: { median_pano_age_years: 4 },
          coverage_rate_percent: 55,
          search_area_km2: 25,
          data_file: "c",
          json_file: "d",
        },
        runs: [{ run_date: "2026-01-01" }, { run_date: "2026-07-05" }],
        change: { from: "2026-01-01", panos_added: 5 },
      },
    },
  };
  const gsv = adaptCityRecord(v3, "gsv");
  assert.equal(gsv.pano_count, 60);
  assert.equal(gsv.runs.length, 2);
  assert.deepEqual(gsv.capture_year_histogram, { counts: { 2020: 5 } });
  // No mapillary block on this city → adapted record is null (omitted upstream).
  assert.equal(adaptCityRecord(v3, "mapillary"), null);
});

// --- issue #116: any-imagery coverage stratification -----------------------

test("adaptCityRecord: mapillary any-imagery coverage is surfaced and exceeds 360°", () => {
  const v3 = {
    city_id: "bend--or",
    city: { name: "Bend", state: "OR", country: "USA" },
    providers: {
      mapillary: {
        latest: {
          run_date: "2026-07-05",
          panorama_counts: { unique_panos: 40 },
          histogram_of_capture_dates_by_year: { all_panos: { counts: { 2021: 3 } } },
          all_panos_age_stats: { median_pano_age_years: 5 },
          coverage_rate_percent: 50,
          any_imagery_coverage_rate_percent: 75,
          num_flat_images: 120,
          search_area_km2: 25,
          data_file: "c",
          json_file: "d",
        },
        runs: [{ run_date: "2026-07-05", coverage_rate_percent: 50,
                 any_imagery_coverage_rate_percent: 75 }],
        change: null,
      },
    },
  };
  const mly = adaptCityRecord(v3, "mapillary");
  assert.equal(mly.coverage_rate_percent, 50);
  assert.equal(mly.any_imagery_coverage_rate_percent, 75);
  assert.equal(mly.num_flat_images, 120);
});

test("adaptCityRecord: any-imagery falls back to 360° when absent (GSV / pre-v7)", () => {
  const v2 = {
    city_id: "x--y",
    city: { name: "X", state: null, country: "USA" },
    latest: {
      run_date: "2026-07-05",
      panorama_counts: { unique_panos: 10, unique_google_panos: 8 },
      histogram_of_capture_dates_by_year: { all_panos: { counts: {} } },
      all_panos_age_stats: { median_pano_age_years: 2 },
      coverage_rate_percent: 88,
      // no any_imagery_coverage_rate_percent, no num_flat_images
      search_area_km2: 9,
      data_file: "c",
      json_file: "d",
    },
    runs: [],
  };
  const gsv = adaptCityRecord(v2, "gsv");
  assert.equal(gsv.any_imagery_coverage_rate_percent, 88); // === 360° rate
  assert.equal(gsv.num_flat_images, null);
});

// --- schema v12: grid size in sample points --------------------------------

test("adaptCityRecord: grid size and geometry are surfaced from a v3 record", () => {
  const v3 = {
    city_id: "corvallis--or",
    city: { name: "Corvallis", state: "OR", country: "USA" },
    providers: {
      gsv: {
        latest: {
          run_date: "2026-07-05",
          panorama_counts: { unique_panos: 10, unique_google_panos: 8 },
          histogram_of_capture_dates_by_year: {},
          all_panos_age_stats: {},
          coverage_rate_percent: 40,
          search_area_km2: 25,
          total_search_points: 62_500,
          grid: { width_meters: 5000, height_meters: 5000, step_length_meters: 20 },
          data_file: "c",
          json_file: "d",
        },
        runs: [],
      },
    },
  };
  const gsv = adaptCityRecord(v3, "gsv");
  // The denominator coverage_rate_percent is a percentage OF: without it a
  // village's 40% and a metro's 40% are indistinguishable.
  assert.equal(gsv.total_search_points, 62_500);
  assert.deepEqual(gsv.grid, {
    width_meters: 5000,
    height_meters: 5000,
    step_length_meters: 20,
  });
});

test("adaptCityRecord: grid keys are null — not undefined, not all-null objects — when absent", () => {
  // v2 records will never carry them, and v1 predates them entirely. Both must
  // read as an explicit null so a consumer can write `if (rec.grid)` and get a
  // sound answer; an object of nulls would be truthy and render "null × null".
  const v2 = {
    city_id: "x--y",
    city: { name: "X", state: null, country: "USA" },
    latest: {
      run_date: "2026-07-05",
      panorama_counts: { unique_panos: 10 },
      histogram_of_capture_dates_by_year: {},
      all_panos_age_stats: {},
      coverage_rate_percent: 88,
      search_area_km2: 9,
      data_file: "c",
      json_file: "d",
    },
    runs: [],
  };
  const fromV2 = adaptCityRecord(v2, "gsv");
  assert.equal(fromV2.total_search_points, null);
  assert.equal(fromV2.grid, null);

  const fromV1 = adaptCityRecord(V1_RECORD, "gsv");
  assert.equal(fromV1.total_search_points, null);
  assert.equal(fromV1.grid, null);
});

test("METRICS.coverage_any: reads any-imagery rate, falls back to 360°", () => {
  const m = METRICS.coverage_any;
  assert.equal(isKnownMetric("coverage_any"), true);
  // Prefers the any-imagery number when present...
  assert.equal(
    m.valueOf({ any_imagery_coverage_rate_percent: 75, coverage_rate_percent: 50 }),
    75
  );
  // ...falls back to the 360° rate when it's missing (GSV / old data)...
  assert.equal(m.valueOf({ coverage_rate_percent: 50 }), 50);
  // ...and is null when neither exists.
  assert.equal(m.valueOf({}), null);
  // Reuses the coverage color scale (provider-independent, like `coverage`).
  assert.equal(m.color(50), METRICS.coverage.color(50));
});

// --- escapeHtml: XSS guard for data-derived strings -------------------------

test("escapeHtml: neutralizes markup in third-party strings", () => {
  // A hostile Mapillary contributor name must not survive as markup.
  assert.equal(
    escapeHtml('<img src=x onerror=alert(1)>'),
    "&lt;img src=x onerror=alert(1)&gt;"
  );
  assert.equal(
    escapeHtml(`"quoted" & 'single' <tag>`),
    "&quot;quoted&quot; &amp; &#39;single&#39; &lt;tag&gt;"
  );
  // Benign strings pass through unchanged.
  assert.equal(escapeHtml("© Mapillary contributor 42"), "© Mapillary contributor 42");
});

test("escapeHtml: non-string inputs are safe", () => {
  assert.equal(escapeHtml(null), "");
  assert.equal(escapeHtml(undefined), "");
  assert.equal(escapeHtml(12345), "12345");
});

// --- isValidRunFilename: ?file= URL-parameter validation --------------------

test("isValidRunFilename: accepts every run-filename generation", () => {
  // Legacy undated
  assert.ok(isValidRunFilename("bend--or_width_5000_height_5000_step_20.csv.gz"));
  // Buggy float step
  assert.ok(isValidRunFilename("bend--or_width_5000_height_5000_step_20.0.csv.gz"));
  // Dated, tokenless (gsv)
  assert.ok(isValidRunFilename("bend--or_width_5000_height_5000_step_20_2026-07-05.csv.gz"));
  // Dated, provider-tagged
  assert.ok(
    isValidRunFilename("bend--or_width_5000_height_5000_step_20_mapillary_2026-07-05.csv.gz")
  );
  // Slug with interior period (st.-louis rule)
  assert.ok(
    isValidRunFilename("st.-louis--mo_width_5000_height_5000_step_20_2026-07-05.csv.gz")
  );
});

test("isValidRunFilename: rejects traversal and non-run artifacts", () => {
  // Path traversal / separators — the attack the validator exists for.
  assert.equal(isValidRunFilename("../../../etc/passwd"), false);
  assert.equal(
    isValidRunFilename("../other/x_width_1_height_1_step_1_2026-01-01.csv.gz"),
    false
  );
  assert.equal(
    isValidRunFilename("a\\b_width_1_height_1_step_1_2026-01-01.csv.gz"),
    false
  );
  // URL metacharacters that could smuggle a query/fragment.
  assert.equal(
    isValidRunFilename("a?b_width_1_height_1_step_1_2026-01-01.csv.gz"),
    false
  );
  // Non-run artifacts: diff files, history files, working files.
  assert.equal(isValidRunFilename("bend--or_diff_2026-04-01_to_2026-07-01.csv.gz"), false);
  assert.equal(
    isValidRunFilename("bend--or_width_5000_height_5000_step_20_gsv_history_2026-07-05.csv.gz"),
    false
  );
  assert.equal(
    isValidRunFilename("bend--or_width_5000_height_5000_step_20_2026-07-05.csv.gz.rejected"),
    false
  );
  assert.equal(isValidRunFilename("cities.json.gz"), false);
  assert.equal(isValidRunFilename(""), false);
  assert.equal(isValidRunFilename(null), false);
});

// --- isKnownProvider / getProviderFromFilename: prototype-safe lookups ------

test("isKnownProvider: real keys yes, prototype members no", () => {
  assert.equal(isKnownProvider("gsv"), true);
  assert.equal(isKnownProvider("mapillary"), true);
  assert.equal(isKnownProvider("kartaview"), false);
  // Object.prototype members are truthy via PROVIDERS[key] but are NOT
  // providers — ?provider=constructor used to break the whole UI.
  assert.equal(isKnownProvider("constructor"), false);
  assert.equal(isKnownProvider("hasOwnProperty"), false);
  assert.equal(isKnownProvider(null), false);
  assert.equal(isKnownProvider(undefined), false);
});

test("getProviderFromFilename: token detection is prototype-safe", () => {
  assert.equal(
    getProviderFromFilename("bend--or_width_5000_height_5000_step_20_2026-07-05.csv.gz"),
    "gsv");
  assert.equal(
    getProviderFromFilename("bend--or_width_5000_height_5000_step_20_mapillary_2026-07-05.csv.gz"),
    "mapillary");
  // Unknown and prototype-member tokens both fall back to gsv.
  assert.equal(
    getProviderFromFilename("bend--or_width_5000_height_5000_step_20_kartaview_2026-07-05.csv.gz"),
    "gsv");
  assert.equal(
    getProviderFromFilename("bend--or_width_5000_height_5000_step_20_constructor_2026-07-05.csv.gz"),
    "gsv");
});

// --- display helpers shared by index.js and city.js -------------------------

test("withAlpha: rgb and rgba inputs gain the alpha; others pass through", () => {
  assert.equal(withAlpha("rgb(253, 141, 60)", 0.3), "rgba(253, 141, 60, 0.3)");
  assert.equal(withAlpha("rgba(1,2,3,0.9)", 0.5), "rgba(1, 2, 3, 0.5)");
  assert.equal(withAlpha("#ff0000", 0.3), "#ff0000");
  assert.equal(withAlpha(null, 0.3), null);
});

test("fmtYears: value and null", () => {
  assert.equal(fmtYears(4.2), "4.2 years");
  assert.equal(fmtYears(0), "0.0 years");
  assert.equal(fmtYears(null), "—");
  assert.equal(fmtYears(undefined), "—");
});

test("formatChangeSummary: full block, minimal block, and absent", () => {
  assert.equal(formatChangeSummary(null), null);
  assert.equal(formatChangeSummary(undefined), null);

  const full = formatChangeSummary({
    from: "2026-01-15",
    panos_added: 1234,
    panos_removed: 56,
    capture_date_changed: 7,
    coverage_delta_pct: -0.25,
  });
  assert.equal(full.from, "2026-01-15");
  assert.equal(full.added, `+${(1234).toLocaleString()} new`);
  assert.equal(full.removed, "−56 removed");
  assert.equal(full.redated, "7 panos re-dated");
  assert.equal(full.coverage, "-0.25 pct points");

  // city.js's per-run JSON uses from_run_date; zero/absent fields degrade
  const minimal = formatChangeSummary({ from_run_date: "2026-04-01" });
  assert.equal(minimal.from, "2026-04-01");
  assert.equal(minimal.added, "+0 new");
  assert.equal(minimal.removed, "−0 removed");
  assert.equal(minimal.redated, null);
  assert.equal(minimal.coverage, null);

  // Positive coverage gets an explicit sign
  assert.equal(
    formatChangeSummary({ from: "x", coverage_delta_pct: 1.5 }).coverage,
    "+1.50 pct points");
});

// --- isGoogleCopyright: exact © Google match -------------------------------

test("isGoogleCopyright: matches only the exact © Google string", () => {
  assert.equal(isGoogleCopyright("© Google"), true);
  // Photographer names can contain "Google" — must NOT match on substring.
  assert.equal(isGoogleCopyright("Google Street View contributor"), false);
  assert.equal(isGoogleCopyright("© Google, Inc"), false);
  assert.equal(isGoogleCopyright("© Jane Doe"), false);
  assert.equal(isGoogleCopyright(null), false);
  assert.equal(isGoogleCopyright(undefined), false);
  assert.equal(isGoogleCopyright(""), false);
});

// --- isPlausibleCaptureDate: the JS mirror of the #213 bound ----------------

test("isPlausibleCaptureDate: rejects impossible dates, keeps real ones", () => {
  assert.equal(isPlausibleCaptureDate(panoDateOrNull("2022-06-01"), "gsv"), true);
  // The two shapes seen in production contributor EXIF
  assert.equal(isPlausibleCaptureDate(panoDateOrNull("2611-09-01"), "gsv"), false);
  assert.equal(isPlausibleCaptureDate(panoDateOrNull("1970-08-01"), "gsv"), false);
  // Absent/unparseable is handled here too, so callers need one check
  assert.equal(isPlausibleCaptureDate(null, "gsv"), false);
  assert.equal(isPlausibleCaptureDate(panoDateOrNull("nonsense"), "gsv"), false);
});

test("isPlausibleCaptureDate: the floor is per-provider, not the color-scale launch date", () => {
  // 2005 predates Street View but is ordinary for Mapillary, whose
  // contributors upload genuinely old photographs.
  assert.equal(isPlausibleCaptureDate(panoDateOrNull("2005-06-01"), "gsv"), false);
  assert.equal(isPlausibleCaptureDate(panoDateOrNull("2005-06-01"), "mapillary"), true);
  // ...and the bound is NOT PROVIDERS[p].launchDate, which anchors the color
  // ramp: 2010 Mapillary imagery predates that date and must still pass.
  assert.ok(panoDateOrNull("2010-01-01") < PROVIDERS.mapillary.launchDate);
  assert.equal(isPlausibleCaptureDate(panoDateOrNull("2010-01-01"), "mapillary"), true);
  // An unknown provider falls back to the loosest floor rather than dropping
  // everything before 2007.
  assert.equal(isPlausibleCaptureDate(panoDateOrNull("2005-06-01"), "who"), true);
});

test("isPlausibleCaptureDate: an absent provider means gsv, not the loose fallback", () => {
  // A default parameter fires only on undefined, so an explicit null or "" —
  // an unset provider threaded in from a caller — used to skip it, miss
  // PROVIDERS, and land on Mapillary's deliberately-loose 2004 floor, quietly
  // accepting a GSV pano dated 2005. Failing open is the wrong direction for a
  // helper whose signature advertises gsv.
  for (const absent of [undefined, null, ""]) {
    assert.equal(isPlausibleCaptureDate(panoDateOrNull("2005-06-01"), absent), false);
    assert.equal(isPlausibleCaptureDate(panoDateOrNull("2022-06-01"), absent), true);
  }
});

test("isPlausibleCaptureDate: the mirrored floors match analysis.EARLIEST_PLAUSIBLE_CAPTURE", () => {
  // Asserted on LOCAL fields, not toISOString(): the floors are built in the
  // same frame as the dates they bound (see below), so a UTC reading of them
  // is off by a day in half the world.
  const floors = [
    [PROVIDERS.gsv.earliestPlausibleCapture, 2007],
    [PROVIDERS.mapillary.earliestPlausibleCapture, 2004],
  ];
  for (const [floor, year] of floors) {
    assert.equal(floor.getFullYear(), year);
    assert.equal(floor.getMonth(), 0);
    assert.equal(floor.getDate(), 1);
  }
});

test("isPlausibleCaptureDate: the floor is inclusive in every timezone", () => {
  // The floor must be built the way panoDateOrNull builds its input — local
  // midnight — or the comparison mixes frames and a capture dated exactly ON
  // the floor is rejected east of UTC while analysis.plausible_capture_mask
  // (an inclusive `between`) keeps it in every published artifact. The map's
  // pano set would then depend on the viewer's timezone.
  for (const p of ["gsv", "mapillary"]) {
    assert.equal(PROVIDERS[p].earliestPlausibleCapture.getHours(), 0);
    const onTheFloor = panoDateOrNull(
      `${PROVIDERS[p].earliestPlausibleCapture.getFullYear()}-01-01`);
    assert.equal(isPlausibleCaptureDate(onTheFloor, p), true);
  }
  // Under TZ=UTC the two spellings coincide, so the assertions above cannot
  // catch a regression on a CI runner that sets no timezone. Re-check in a
  // zone on each side of UTC, in a child process — TZ has to be set before the
  // module builds its Date constants.
  const probe = `
    const {panoDateOrNull, isPlausibleCaptureDate} = require(${JSON.stringify(
      require.resolve("../streetscape-utils.js"))});
    const ok = isPlausibleCaptureDate(panoDateOrNull("2007-01-01"), "gsv")
      && isPlausibleCaptureDate(panoDateOrNull("2004-01-01"), "mapillary");
    process.stdout.write(ok ? "inclusive" : "EXCLUSIVE");
  `;
  for (const tz of ["Asia/Tokyo", "America/Los_Angeles", "UTC"]) {
    const out = execFileSync(process.execPath, ["-e", probe], {
      env: { ...process.env, TZ: tz },
      encoding: "utf8",
    });
    assert.equal(out, "inclusive", `floor is exclusive under TZ=${tz}`);
  }
});

// --- googleSharePercent: divide-by-zero guard (B1–B4) ----------------------

test("googleSharePercent: normal and 0-total (no Infinity%)", () => {
  assert.equal(googleSharePercent(60, 100), "60.0");
  assert.equal(googleSharePercent(1, 3), "33.3");
  // A 0-pano run must render "0.0", never "Infinity" or "NaN".
  assert.equal(googleSharePercent(0, 0), "0.0");
  assert.equal(googleSharePercent(3, 0), "0.0");
});

// --- buildFilledHistogram: empty/missing guard (#69) -----------------------

test("buildFilledHistogram: gap-fills through currentYear", () => {
  assert.deepEqual(buildFilledHistogram({ 2018: 2, 2020: 5 }, 2021), {
    2018: 2,
    2019: 0,
    2020: 5,
    2021: 0,
  });
});

test("buildFilledHistogram: empty/missing histogram yields {} (no Infinity loop)", () => {
  assert.deepEqual(buildFilledHistogram({}, 2021), {});
  assert.deepEqual(buildFilledHistogram(undefined, 2021), {});
  assert.deepEqual(buildFilledHistogram(null, 2021), {});
});

// --- panoDateOrNull: epoch guard (#122 / #69) ------------------------------

test("panoDateOrNull: falsy inputs return null, not the Unix epoch", () => {
  assert.equal(panoDateOrNull(null), null);
  assert.equal(panoDateOrNull(undefined), null);
  assert.equal(panoDateOrNull(""), null);
  assert.equal(panoDateOrNull(0), null);
});

test("panoDateOrNull: valid ISO date parses to a Date", () => {
  const d = panoDateOrNull("2020-06-15");
  assert.ok(d instanceof Date);
  assert.ok(!Number.isNaN(d.getTime()));
  assert.equal(d.getUTCFullYear(), 2020);
});

// --- getColor: YlOrRd age color scale boundaries ----------------------------
//
// The scale's documented stops: age 0 → rgb(255, 255, 178) (light yellow),
// age max/2 → rgb(253, 141, 60) (orange), age >= provider max →
// rgb(189, 0, 38) (dark red). The provider max is wall-clock-derived
// (years since the provider's launch), so tests pin the stops with ages
// that are max-independent: 0, an age far beyond any max, and max/2
// recomputed from the exported PROVIDERS launch dates.

test("getColor: age 0 is the light-yellow newest stop for every provider", () => {
  assert.equal(getColor(0), "rgb(255, 255, 178)");
  assert.equal(getColor(0, "gsv"), "rgb(255, 255, 178)");
  assert.equal(getColor(0, "mapillary"), "rgb(255, 255, 178)");
});

test("getColor: ages at/beyond the provider max clamp to dark red", () => {
  // 1e6 years dwarfs any provider's launch-anchored max, so the ratio must
  // clamp to 1 instead of extrapolating past the dark-red stop.
  assert.equal(getColor(1e6, "gsv"), "rgb(189, 0, 38)");
  assert.equal(getColor(1e6, "mapillary"), "rgb(189, 0, 38)");
});

test("getColor: half the provider max is the orange middle stop", () => {
  // Recompute the provider max exactly as the module does (years since
  // launch, 365.25-day years). Both interpolation branches meet at the
  // 0.5 boundary with the same color, so the sub-millisecond clock drift
  // between module load and this call cannot flip the rounded result.
  const msPerYear = 1000 * 60 * 60 * 24 * 365.25;
  const gsvMax = (Date.now() - PROVIDERS.gsv.launchDate.getTime()) / msPerYear;
  assert.equal(getColor(gsvMax / 2, "gsv"), "rgb(253, 141, 60)");
});

test("getColor: unknown provider falls back to the gsv scale", () => {
  assert.equal(getColor(7, "kartaview"), getColor(7, "gsv"));
});

test("getColor: null age (0-pano run) coerces to the newest stop, not NaN", () => {
  // index.js passes pano_age_stats.median_pano_age_years straight through,
  // and that field is null for a 0-pano run. null/maxAge coerces to 0, so
  // the color must be the valid age-0 stop — never "rgb(NaN, NaN, NaN)".
  assert.equal(getColor(null), "rgb(255, 255, 178)");
  assert.equal(getColor(null, "mapillary"), "rgb(255, 255, 178)");
});

// --- coverageColor: sequential teal coverage scale ---------------------------

test("coverageColor: exact stops at 0%, 50%, and 100%", () => {
  assert.equal(coverageColor(0), "rgb(21, 86, 97)");
  assert.equal(coverageColor(50), "rgb(69, 170, 176)");
  assert.equal(coverageColor(100), "rgb(127, 244, 227)");
});

test("coverageColor: out-of-range percentages clamp to the end stops", () => {
  assert.equal(coverageColor(-5), coverageColor(0));
  assert.equal(coverageColor(150), coverageColor(100));
});

test("coverageColor: lightness increases monotonically with coverage", () => {
  // Higher coverage must always read brighter on the dark basemap. Use the
  // channel sum as a lightness proxy — sufficient for a single-hue ramp.
  const lightness = (pct) => {
    const [, r, g, b] = /^rgb\((\d+), (\d+), (\d+)\)$/.exec(coverageColor(pct));
    return Number(r) + Number(g) + Number(b);
  };
  for (let pct = 10; pct <= 100; pct += 10) {
    assert.ok(lightness(pct) > lightness(pct - 10),
      `coverageColor(${pct}) must be lighter than coverageColor(${pct - 10})`);
  }
});

// --- METRICS / isKnownMetric: the "color by" registry ------------------------

test("isKnownMetric: real keys yes, prototype members no", () => {
  assert.equal(isKnownMetric("age"), true);
  assert.equal(isKnownMetric("coverage"), true);
  assert.equal(isKnownMetric("panos"), false);
  // ?metric= is attacker-controlled: Object.prototype member names must
  // not pass (same guard as isKnownProvider).
  assert.equal(isKnownMetric("constructor"), false);
  assert.equal(isKnownMetric("hasOwnProperty"), false);
  assert.equal(isKnownMetric(null), false);
  assert.equal(isKnownMetric(undefined), false);
});

test("METRICS.age: valueOf null-guards missing age stats", () => {
  assert.equal(
    METRICS.age.valueOf({ pano_age_stats: { median_pano_age_years: 4.2 } }),
    4.2);
  assert.equal(METRICS.age.valueOf({ pano_age_stats: {} }), null);
  assert.equal(METRICS.age.valueOf({}), null);
});

test("METRICS.age: buckets and labels match the historical legend", () => {
  assert.equal(METRICS.age.bucketOf(4.9), 4);
  assert.equal(METRICS.age.bucketLabel(1), "1 year");
  assert.equal(METRICS.age.bucketLabel(3), "3 years");
  // 0..ceil(max) ascending, and safe on an all-no-data provider view
  assert.deepEqual(METRICS.age.legendBuckets([0.5, 7.2]),
    [0, 1, 2, 3, 4, 5, 6, 7, 8]);
  assert.deepEqual(METRICS.age.legendBuckets([]), [0]);
});

test("METRICS.coverage: valueOf, decile buckets, and labels", () => {
  assert.equal(METRICS.coverage.valueOf({ coverage_rate_percent: 51.2 }), 51.2);
  assert.equal(METRICS.coverage.valueOf({}), null);
  assert.equal(METRICS.coverage.bucketOf(0), 0);
  assert.equal(METRICS.coverage.bucketOf(9.99), 0);
  assert.equal(METRICS.coverage.bucketOf(10), 1);
  assert.equal(METRICS.coverage.bucketOf(95), 9);
  // 100% must fold into the top decile, not a phantom bucket 10
  assert.equal(METRICS.coverage.bucketOf(100), 9);
  assert.equal(METRICS.coverage.bucketLabel(0), "0–10%");
  assert.equal(METRICS.coverage.bucketLabel(9), "90–100%");
  // Best coverage listed first
  assert.deepEqual(METRICS.coverage.legendBuckets([]),
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]);
  assert.equal(METRICS.coverage.formatValue(51.25), "51.3%");
});

test("METRICS rangeLabel: age singular/plural, coverage decile edges", () => {
  // Single-bucket ranges read like the bucket rows themselves
  assert.equal(METRICS.age.rangeLabel(1, 1), "1 year");
  assert.equal(METRICS.age.rangeLabel(2, 2), "2 years");
  assert.equal(METRICS.age.rangeLabel(2, 5), "2–5 years");
  // Decile bucket b spans [10b, 10b+10) — a single decile matches its
  // bucketLabel, and the top bucket's upper edge is 100%, never 90%
  assert.equal(METRICS.coverage.rangeLabel(3, 3),
    METRICS.coverage.bucketLabel(3));
  assert.equal(METRICS.coverage.rangeLabel(2, 7), "20–80%");
  assert.equal(METRICS.coverage.rangeLabel(0, 9), "0–100%");
});

test("parseFilterParam: valid ranges parse, hostile input is rejected", () => {
  // URL-supplied ?filter=MIN-MAX against an age span of buckets 0..18
  assert.deepEqual(parseFilterParam("2-5", 0, 18), { min: 2, max: 5 });
  assert.deepEqual(parseFilterParam("0-18", 0, 18), { min: 0, max: 18 });
  assert.deepEqual(parseFilterParam("7-7", 0, 9), { min: 7, max: 7 });

  // Rejected (null), never clamped or half-applied
  assert.equal(parseFilterParam("5-2", 0, 18), null);   // inverted
  assert.equal(parseFilterParam("2-25", 0, 18), null);  // beyond span
  assert.equal(parseFilterParam("-1-3", 0, 18), null);  // negative / malformed
  assert.equal(parseFilterParam("2-", 0, 18), null);
  assert.equal(parseFilterParam("2-5-7", 0, 18), null);
  assert.equal(parseFilterParam("abc", 0, 18), null);
  assert.equal(parseFilterParam("", 0, 18), null);
  assert.equal(parseFilterParam(null, 0, 18), null);
  assert.equal(parseFilterParam(undefined, 0, 18), null);
});

test("panoDateOrNull: date-only strings are local calendar dates (no TZ shift)", () => {
  // Regression: `new Date("2023-01-01")` is UTC midnight, so local getters
  // west of UTC (TZ pinned to America/Los_Angeles above) read it back as
  // Dec 31, 2022 — putting every Jan/year-precision capture date in the
  // previous year's filter bucket and color.
  const d = panoDateOrNull("2023-01-01");
  assert.equal(d.getFullYear(), 2023);
  assert.equal(d.getMonth(), 0);
  assert.equal(d.getDate(), 1);
  assert.equal(d.toLocaleDateString("en-US"), "1/1/2023");
  // Full timestamps (with a time component) keep native parsing.
  const ts = panoDateOrNull("2026-07-05T12:34:56+00:00");
  assert.ok(ts instanceof Date && !Number.isNaN(ts.getTime()));
  assert.equal(ts.getUTCHours(), 12);
});

// --- Render cap: spatialStrideSample (issues #77/#58) ---------------------

test("spatialStrideSample: returns all indices when total <= cap", () => {
  const pts = [[0, 0], [1, 1], [2, 2]];
  assert.deepEqual(spatialStrideSample(pts, 5), [0, 1, 2]);
  assert.deepEqual(spatialStrideSample(pts, 3), [0, 1, 2]); // exactly at cap
  assert.deepEqual(spatialStrideSample([], 10), []);
});

test("spatialStrideSample: caps to exactly `cap` indices when total > cap", () => {
  const pts = Array.from({ length: 100 }, (_, i) => [i, 0]);
  const idx = spatialStrideSample(pts, 10);
  assert.equal(idx.length, 10);
  // Every index is valid and unique.
  assert.equal(new Set(idx).size, 10);
  idx.forEach((i) => assert.ok(i >= 0 && i < 100));
});

test("spatialStrideSample: guards non-positive / non-finite cap", () => {
  const pts = [[0, 0], [1, 1]];
  assert.deepEqual(spatialStrideSample(pts, 0), []);
  assert.deepEqual(spatialStrideSample(pts, -5), []);
  assert.deepEqual(spatialStrideSample(pts, Infinity), []);
  assert.deepEqual(spatialStrideSample(pts, NaN), []);
});

test("spatialStrideSample: deterministic for the same input", () => {
  const pts = Array.from({ length: 50 }, (_, i) => [Math.sin(i), Math.cos(i)]);
  assert.deepEqual(spatialStrideSample(pts, 12), spatialStrideSample(pts, 12));
});

test("spatialStrideSample: subset spans the full spatial extent (no clumping)", () => {
  // Points laid out along a diagonal; a head-of-list or single-corner sample
  // would collapse the lat/lon range. The stride must keep near-full spread.
  const n = 1000;
  const pts = Array.from({ length: n }, (_, i) => [i / n, i / n]);
  const idx = spatialStrideSample(pts, 20);
  const lats = idx.map((i) => pts[i][0]);
  assert.ok(Math.min(...lats) <= 0.05, "samples reach the low end");
  assert.ok(Math.max(...lats) >= 0.95, "samples reach the high end");
});

test("spatialStrideSample: orders by latitude then longitude before striding", () => {
  // Reverse-sorted input; a cap of 2 over 4 points strides the SORTED order,
  // so it must pick from the low end first, not the input's head.
  const pts = [[9, 0], [8, 0], [1, 0], [0, 0]];
  const idx = spatialStrideSample(pts, 2);
  const lats = idx.map((i) => pts[i][0]).sort((a, b) => a - b);
  // Lowest-latitude point (0) is always the first stride pick.
  assert.equal(lats[0], 0);
});

// --- Render cap: computeVisibilityDelta -----------------------------------

test("computeVisibilityDelta: identical sets → no work", () => {
  const a = {}, b = {}, c = {};
  const onMap = new Set([a, b, c]);
  const { toAdd, toRemove } = computeVisibilityDelta(onMap, new Set([a, b, c]));
  assert.deepEqual(toAdd, []);
  assert.deepEqual(toRemove, []);
});

test("computeVisibilityDelta: empty on-map → add everything (initial render)", () => {
  const a = {}, b = {};
  const { toAdd, toRemove } = computeVisibilityDelta(new Set(), [a, b]);
  assert.deepEqual(new Set(toAdd), new Set([a, b]));
  assert.deepEqual(toRemove, []);
});

test("computeVisibilityDelta: disjoint sets → full swap", () => {
  const a = {}, b = {}, c = {}, d = {};
  const { toAdd, toRemove } = computeVisibilityDelta(new Set([a, b]), new Set([c, d]));
  assert.deepEqual(new Set(toAdd), new Set([c, d]));
  assert.deepEqual(new Set(toRemove), new Set([a, b]));
});

test("computeVisibilityDelta: overlap → only the difference moves", () => {
  const a = {}, b = {}, c = {};
  const { toAdd, toRemove } = computeVisibilityDelta(new Set([a, b]), new Set([b, c]));
  assert.deepEqual(toAdd, [c]);
  assert.deepEqual(toRemove, [a]);
});

test("computeVisibilityDelta: accepts an array target", () => {
  const a = {}, b = {};
  const { toAdd, toRemove } = computeVisibilityDelta(new Set([a]), [a, b]);
  assert.deepEqual(toAdd, [b]);
  assert.deepEqual(toRemove, []);
});

// --- Render cap: markerDateStyle ------------------------------------------

test("markerDateStyle: no selected date → default style", () => {
  assert.deepEqual(markerDateStyle("Mon Jan 02 2023", null), { fillOpacity: 0.8, radius: 3 });
  assert.deepEqual(markerDateStyle("Mon Jan 02 2023", ""), { fillOpacity: 0.8, radius: 3 });
});

test("markerDateStyle: matching date → emphasized", () => {
  assert.deepEqual(
    markerDateStyle("Mon Jan 02 2023", "Mon Jan 02 2023"),
    { fillOpacity: 1, radius: 4 },
  );
});

test("markerDateStyle: non-matching date → dimmed", () => {
  assert.deepEqual(
    markerDateStyle("Tue Jan 03 2023", "Mon Jan 02 2023"),
    { fillOpacity: 0.05, radius: 3 },
  );
});

test("RENDER_CAP is a positive finite number", () => {
  assert.ok(Number.isFinite(RENDER_CAP) && RENDER_CAP > 0);
});

// --- Streetwalk manifest (issue #155) --------------------------------------
//
// These helpers moved here from street-coverage.js when the overview map and
// streets.html grew a need for them. fetchGzippedJson is now an in-module
// call, so the fetch tests stub the browser primitives (fetch + pako) rather
// than the helper itself.

/** Serve one JSON payload through the fetch+pako path fetchGzippedJson uses. */
function stubGzippedFetch(payload, { ok = true } = {}) {
  const seen = [];
  global.pako = { inflate: (bytes) => Buffer.from(bytes).toString("utf8") };
  global.fetch = async (url) => {
    seen.push(url);
    return {
      ok,
      status: ok ? 200 : 404,
      arrayBuffer: async () => Buffer.from(JSON.stringify(payload), "utf8"),
    };
  };
  return seen;
}

function restoreGzippedFetch() {
  delete global.pako;
  delete global.fetch;
}

test("streetwalkManifestUrl points at streetwalks.json.gz under the data base URL", () => {
  assert.equal(
    streetwalkManifestUrl(),
    STREETSCAPE_DATA_BASE_URL + "streetwalks.json.gz"
  );
});

test("fetchStreetwalkManifest: reads streetwalks.json.gz from the data base URL", async () => {
  const seen = stubGzippedFetch({
    schema_version: 1,
    walks: [{ city_id: "bend--or", provider: "gsv" }],
  });
  try {
    const manifest = await fetchStreetwalkManifest();
    assert.deepEqual(seen, [STREETSCAPE_DATA_BASE_URL + "streetwalks.json.gz"]);
    assert.equal(manifest.walks.length, 1);
  } finally {
    restoreGzippedFetch();
  }
});

test("fetchStreetwalkManifest: a missing/unreadable manifest resolves null, never throws", async () => {
  // Most deployments have no manifest yet — the road-walk layer is optional, so
  // a 404 must degrade to "no street coverage" rather than reject into the page.
  stubGzippedFetch({}, { ok: false });
  try {
    assert.equal(await fetchStreetwalkManifest(), null);
  } finally {
    restoreGzippedFetch();
  }
});

test("lookupStreetwalk: finds by city_id+provider, null on miss or absent manifest", () => {
  const manifest = {
    walks: [
      { city_id: "seattle--wa", provider: "gsv", coverage_filename: "seattle_gsv.json.gz" },
      { city_id: "seattle--wa", provider: "mapillary", coverage_filename: "seattle_mly.json.gz" },
    ],
  };
  assert.equal(
    lookupStreetwalk(manifest, "seattle--wa", "gsv").coverage_filename,
    "seattle_gsv.json.gz"
  );
  assert.equal(
    lookupStreetwalk(manifest, "seattle--wa", "mapillary").coverage_filename,
    "seattle_mly.json.gz"
  );
  assert.equal(lookupStreetwalk(manifest, "portland--or", "gsv"), null);
  assert.equal(lookupStreetwalk(null, "seattle--wa", "gsv"), null);
  assert.equal(lookupStreetwalk({}, "seattle--wa", "gsv"), null);
});

test("lookupStreetwalk: selects on network type, defaulting to drive", () => {
  // A city can carry one walk per network type. Taking the first match would
  // render whichever the manifest happened to list first — and drive vs
  // all_public coverage divide by different street-km denominators.
  const manifest = {
    walks: [
      {
        city_id: "seattle--wa",
        provider: "gsv",
        network_type: "all_public",
        coverage_filename: "seattle_gsv_allpublic.json.gz",
      },
      {
        city_id: "seattle--wa",
        provider: "gsv",
        network_type: "drive",
        coverage_filename: "seattle_gsv_drive.json.gz",
      },
    ],
  };
  assert.equal(
    lookupStreetwalk(manifest, "seattle--wa", "gsv").coverage_filename,
    "seattle_gsv_drive.json.gz"
  );
  assert.equal(
    lookupStreetwalk(manifest, "seattle--wa", "gsv", "all_public").coverage_filename,
    "seattle_gsv_allpublic.json.gz"
  );
  assert.equal(lookupStreetwalk(manifest, "seattle--wa", "gsv", "walk"), null);
});

test("lookupStreetwalk: a walk with no network_type is a drive walk", () => {
  // Manifest entries published before network types existed carry no field.
  const manifest = {
    walks: [{ city_id: "seattle--wa", provider: "gsv", coverage_filename: "legacy.json.gz" }],
  };
  assert.equal(lookupStreetwalk(manifest, "seattle--wa", "gsv").coverage_filename, "legacy.json.gz");
  assert.equal(lookupStreetwalk(manifest, "seattle--wa", "gsv", "all_public"), null);
});

test("mergeStreetwalkStats: joins by city_id+provider and counts the matches", () => {
  const cities = [
    { city_id: "seattle--wa", provider: "gsv" },
    { city_id: "bend--or", provider: "gsv" },
  ];
  const manifest = {
    walks: [
      { city_id: "seattle--wa", provider: "gsv", coverage_pct_by_length: 98.4 },
    ],
  };
  assert.equal(mergeStreetwalkStats(cities, manifest), 1);
  assert.equal(cities[0].street_coverage_pct_by_length, 98.4);
  assert.equal(cities[0].street_walk.coverage_pct_by_length, 98.4);
  // Unwalked cities get explicit nulls, not missing keys, so METRICS.streets
  // and the popup both read a defined property.
  assert.equal(cities[1].street_coverage_pct_by_length, null);
  assert.equal(cities[1].street_walk, null);
});

test("mergeStreetwalkStats: a walk for the other provider does not leak across", () => {
  // Each provider is an independent run series on the same grid — a Mapillary
  // walk must never color the GSV view (and today there are no Mapillary walks).
  const cities = [{ city_id: "seattle--wa", provider: "gsv" }];
  const manifest = {
    walks: [
      { city_id: "seattle--wa", provider: "mapillary", coverage_pct_by_length: 42 },
    ],
  };
  assert.equal(mergeStreetwalkStats(cities, manifest), 0);
  assert.equal(cities[0].street_coverage_pct_by_length, null);
});

test("mergeStreetwalkStats: a null manifest leaves every city unwalked", () => {
  const cities = [{ city_id: "seattle--wa", provider: "gsv" }];
  assert.equal(mergeStreetwalkStats(cities, null), 0);
  assert.equal(cities[0].street_coverage_pct_by_length, null);
});

test("mergeStreetwalkStats: a malformed manifest is treated as no walks", () => {
  const cities = [{ city_id: "seattle--wa", provider: "gsv" }];
  assert.equal(mergeStreetwalkStats(cities, {}), 0);
  assert.equal(mergeStreetwalkStats(cities, { walks: "nope" }), 0);
  assert.equal(cities[0].street_walk, null);
});

test("mergeStreetwalkStats: duplicate manifest keys keep the first walk", () => {
  // The manifest is latest-per-(city, provider) by construction, so duplicates
  // shouldn't occur — but the join indexes the walks rather than scanning them
  // per city, and that index must resolve a duplicate the same way the old
  // `find` did, so a malformed manifest can't change what the map shows.
  const cities = [{ city_id: "seattle--wa", provider: "gsv" }];
  const manifest = {
    walks: [
      { city_id: "seattle--wa", provider: "gsv", coverage_pct_by_length: 98.4 },
      { city_id: "seattle--wa", provider: "gsv", coverage_pct_by_length: 11.1 },
    ],
  };
  assert.equal(mergeStreetwalkStats(cities, manifest), 1);
  assert.equal(cities[0].street_coverage_pct_by_length, 98.4);
});

test("mergeStreetwalkStats: only drive walks feed the overview metric", () => {
  // METRICS.streets compares cities to each other. A broad walk's coverage %
  // divides by a much larger street-km denominator, so joining it would put two
  // incompatible scales in one choropleth. A city with only a broad walk is
  // honestly "not measured" for the drive metric.
  const cities = [
    { city_id: "seattle--wa", provider: "gsv" },
    { city_id: "corvallis--or", provider: "gsv" },
  ];
  const manifest = {
    walks: [
      {
        city_id: "seattle--wa",
        provider: "gsv",
        network_type: "all_public",
        coverage_pct_by_length: 61.2,
      },
      {
        city_id: "seattle--wa",
        provider: "gsv",
        network_type: "drive",
        coverage_pct_by_length: 98.4,
      },
      {
        city_id: "corvallis--or",
        provider: "gsv",
        network_type: "all_public",
        coverage_pct_by_length: 44.0,
      },
    ],
  };
  assert.equal(mergeStreetwalkStats(cities, manifest), 1);
  assert.equal(cities[0].street_coverage_pct_by_length, 98.4);
  // Walked, but not on the drive network — so no drive number to show.
  assert.equal(cities[1].street_coverage_pct_by_length, null);
  assert.equal(cities[1].street_walk, null);
});

test("mergeStreetwalkStats: joins every city once at catalog scale", () => {
  // Both sides grow to ~1,150 cities x 2 providers now that street collection
  // is scheduled, and this re-runs on every provider/metric toggle — so the
  // join is indexed rather than a scan per city. Correctness at size is what
  // this asserts; the speedup is the reason.
  const cities = [];
  const walks = [];
  for (let i = 0; i < 1200; i += 1) {
    for (const provider of ["gsv", "mapillary"]) {
      cities.push({ city_id: `city-${i}`, provider });
      // Only every third city has been walked.
      if (i % 3 === 0) {
        walks.push({ city_id: `city-${i}`, provider, coverage_pct_by_length: i % 100 });
      }
    }
  }
  const matched = mergeStreetwalkStats(cities, { walks });
  assert.equal(matched, walks.length);
  assert.equal(cities[0].street_coverage_pct_by_length, 0);
  assert.equal(cities[2].street_walk, null); // city-1, unwalked
});

test("METRICS.streets: reads the merged manifest value, shares coverage's buckets", () => {
  const metric = METRICS.streets;
  assert.equal(metric.valueOf({ street_coverage_pct_by_length: 98.4 }), 98.4);
  // NOT a fallback to grid coverage — a different denominator entirely
  // (street-km driven vs. grid points with imagery), so an unwalked city is
  // "no data", never its grid rate.
  assert.equal(metric.valueOf({ coverage_rate_percent: 77 }), null);
  assert.equal(metric.formatValue(98.4), "98.4%");
  assert.equal(metric.bucketOf(98.4), 9);
  assert.equal(metric.bucketLabel(9), "90–100%");
});

// --- METRICS.freshness: months-since-collection recency buckets --------------

test("METRICS.freshness: valueOf is months since latest_run_date, null when absent", () => {
  const metric = METRICS.freshness;
  assert.equal(metric.valueOf({}), null);
  assert.equal(metric.valueOf({ latest_run_date: null }), null);
  // A run dated today is ~0 months old.
  const today = new Date();
  const iso = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  const months = metric.valueOf({ latest_run_date: iso });
  assert.ok(months >= 0 && months < 1, `expected ~0 months, got ${months}`);
});

test("METRICS.freshness: bucket boundaries land at 3/6/12/18 months", () => {
  const metric = METRICS.freshness;
  assert.equal(metric.bucketOf(0), 0);
  assert.equal(metric.bucketOf(3), 0); // inclusive upper edge
  assert.equal(metric.bucketOf(3.1), 1);
  assert.equal(metric.bucketOf(6), 1);
  assert.equal(metric.bucketOf(12), 2);
  assert.equal(metric.bucketOf(18), 3);
  assert.equal(metric.bucketOf(19), 4);
  assert.equal(metric.bucketOf(200), 4); // no age falls off the scale
});

test("METRICS.freshness: fixed non-negative-integer buckets, freshest first", () => {
  // parseFilterParam only accepts non-negative integers, so the slider/URL
  // machinery works iff every bucket id is one.
  const buckets = METRICS.freshness.legendBuckets([1, 40]);
  assert.deepEqual(buckets, [0, 1, 2, 3, 4]);
  for (const b of buckets) {
    assert.ok(Number.isInteger(b) && b >= 0);
    assert.equal(typeof METRICS.freshness.bucketLabel(b), "string");
  }
  assert.equal(METRICS.freshness.bucketLabel(0), "Last 3 months");
  assert.equal(buckets.length, FRESHNESS_BUCKETS.length);
});

test("recencyColor: five distinct colors, clamped outside the bucket range", () => {
  const colors = [0, 1, 2, 3, 4].map(recencyColor);
  assert.equal(new Set(colors).size, 5);
  assert.equal(recencyColor(-1), recencyColor(0));
  assert.equal(recencyColor(99), recencyColor(4));
});

test("METRICS.freshness: formatValue reads as a recency, not a bare number", () => {
  assert.equal(METRICS.freshness.formatValue(0.4), "collected this month");
  assert.equal(METRICS.freshness.formatValue(17.5), "collected 17.5 months ago");
});

// --- diffFilenameFor / isValidDiffFilename (change-overlay plumbing) ---------

test("diffFilenameFor: GSV is tokenless, other providers carry their token", () => {
  // Mirrors diff.generate_diff_filename (diff.py) — the provider token sits
  // between "diff" and the date pair, and gsv emits none so pre-provider
  // names stay stable.
  assert.equal(
    diffFilenameFor("bend--or", "gsv", "2026-04-01", "2026-07-01"),
    "bend--or_diff_2026-04-01_to_2026-07-01.csv.gz"
  );
  assert.equal(
    diffFilenameFor("bend--or", "mapillary", "2026-04-01", "2026-07-01"),
    "bend--or_diff_mapillary_2026-04-01_to_2026-07-01.csv.gz"
  );
});

test("isValidDiffFilename: accepts published diff names, dotted city ids included", () => {
  assert.ok(isValidDiffFilename("bend--or_diff_2026-04-01_to_2026-07-01.csv.gz"));
  assert.ok(isValidDiffFilename("bend--or_diff_mapillary_2026-04-01_to_2026-07-01.csv.gz"));
  // sanitize_city_query_str preserves interior periods (st.-louis).
  assert.ok(isValidDiffFilename("st.-louis--mo_diff_2025-02-03_to_2026-07-01.csv.gz"));
  // Constructed and validated names agree by construction.
  assert.ok(isValidDiffFilename(diffFilenameFor("a--b", "gsv", "2025-01-01", "2025-04-01")));
});

test("isValidDiffFilename: rejects traversal, hostile chars, and non-diff names", () => {
  assert.equal(isValidDiffFilename(null), false);
  assert.equal(isValidDiffFilename(""), false);
  assert.equal(isValidDiffFilename("../../../etc/passwd"), false);
  assert.equal(isValidDiffFilename("a/b_diff_2026-04-01_to_2026-07-01.csv.gz"), false);
  assert.equal(isValidDiffFilename("a\\b_diff_2026-04-01_to_2026-07-01.csv.gz"), false);
  assert.equal(isValidDiffFilename("a?x=1_diff_2026-04-01_to_2026-07-01.csv.gz"), false);
  assert.equal(isValidDiffFilename("a#f_diff_2026-04-01_to_2026-07-01.csv.gz"), false);
  assert.equal(isValidDiffFilename("bend--or_diff_2026-04-01.csv.gz"), false); // no _to_ pair
  assert.equal(isValidDiffFilename("bend--or_diff_2026-04-01_to_2026-07-01.csv"), false);
});

test("run and diff filename contracts stay disjoint", () => {
  // ?file= must never fetch a diff, and the diff path must never accept a
  // run file — each validator rejects the other's names.
  const runName = "bend--or_width_5000_height_5000_step_20_2026-07-05.csv.gz";
  const diffName = "bend--or_diff_2026-04-01_to_2026-07-01.csv.gz";
  assert.ok(isValidRunFilename(runName));
  assert.equal(isValidDiffFilename(runName), false);
  assert.ok(isValidDiffFilename(diffName));
  assert.equal(isValidRunFilename(diffName), false);
});
