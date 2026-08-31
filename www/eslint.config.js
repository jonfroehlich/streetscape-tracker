// ESLint flat config for the Streetscape Tracker static frontend (issue #123).
//
// The site loads the three js/*.js files as plain browser <script> tags (no
// bundler, no ES modules), so they share one global scope: helpers defined in
// streetscape-utils.js are globals to index.js/city.js, and the vendored libraries
// (Leaflet, Chart.js, PapaParse, pako, moment) are globals to all of them.
// We declare those explicitly so `no-undef` catches real typos (the B1–B4
// undefined/NaN class) without false-flagging the intentional shared globals.
//
// Run with `npm run lint` (or `npx eslint js`) — no node_modules committed.

const js = require("@eslint/js");
const globals = require("globals");

// Vendored libraries loaded via CDN <script> tags in index.html / city.html,
// plus the Google Analytics inline snippet — globals to every browser script.
const vendorGlobals = {
  L: "readonly",
  Chart: "readonly",
  Papa: "readonly",
  pako: "readonly",
  moment: "readonly",
  gtag: "readonly",
  dataLayer: "readonly",
};

// Public symbols streetscape-utils.js DEFINES and the other two scripts CONSUME as
// globals (streetscape-utils.js is loaded first). Declared only for the consumers so
// streetscape-utils.js's own definitions aren't flagged as `no-redeclare`.
const sharedGlobals = {
  STREETSCAPE_DATA_BASE_URL: "readonly",
  addBasemapLayer: "readonly",
  RENDER_CAP: "readonly",
  PROVIDERS: "readonly",
  METRICS: "readonly",
  isKnownProvider: "readonly",
  isKnownMetric: "readonly",
  parseFilterParam: "readonly",
  getColor: "readonly",
  coverageColor: "readonly",
  recencyColor: "readonly",
  FRESHNESS_BUCKETS: "readonly",
  escapeHtml: "readonly",
  isValidRunFilename: "readonly",
  diffFilenameFor: "readonly",
  isValidDiffFilename: "readonly",
  getProviderFromFilename: "readonly",
  fetchGzippedJson: "readonly",
  fetchGzippedText: "readonly",
  streetwalkManifestUrl: "readonly",
  fetchStreetwalkManifest: "readonly",
  lookupStreetwalk: "readonly",
  DEFAULT_STREET_NETWORK_TYPE: "readonly",
  STREET_NETWORK_LABELS: "readonly",
  streetNetworkLabel: "readonly",
  isKnownStreetNetworkType: "readonly",
  mergeStreetwalkStats: "readonly",
  adaptCityRecord: "readonly",
  adaptCitiesPayload: "readonly",
  isGoogleCopyright: "readonly",
  isPlausibleCaptureDate: "readonly",
  panoDateOrNull: "readonly",
  googleSharePercent: "readonly",
  buildFilledHistogram: "readonly",
  withAlpha: "readonly",
  fmtYears: "readonly",
  formatChangeSummary: "readonly",
  spatialStrideSample: "readonly",
  computeVisibilityDelta: "readonly",
  markerDateStyle: "readonly",
};

// Symbols table-utils.js DEFINES and the table pages (grid.js, streets.js,
// driving.js) CONSUME as globals (table-utils.js is loaded between
// streetscape-utils.js and the page script).
const tableGlobals = {
  cityDisplayLabel: "readonly",
  sortRowsBy: "readonly",
  formatCellNumber: "readonly",
  coverageCellHtml: "readonly",
  coverageCellParts: "readonly",
  providerShortLabel: "readonly",
  anyImageryLeafTitle: "readonly",
  SCOPE_MULTI: "readonly",
  scopedProvider: "readonly",
  scopedNumericFilter: "readonly",
  deltaCellHtml: "readonly",
  providerCellHtml: "readonly",
  providerColumnGroup: "readonly",
  headerCellHtml: "readonly",
  theadHtml: "readonly",
  rowHtmlFromColumns: "readonly",
  createSortableTable: "readonly",
};

// Symbols histogram-slider.js DEFINES (issue #250). Loaded between
// table-utils.js (whose formatCellNumber it consumes) and table-controls.js,
// which instantiates the component. All three table pages load it — every
// numeric filter on the site is a histogram-range.
const histogramSliderGlobals = {
  HISTOGRAM_SLIDER_BUCKETS: "readonly",
  sliderStepFor: "readonly",
  normalizeSliderRange: "readonly",
  classifyBuckets: "readonly",
  sliderValuetext: "readonly",
  roundSliderValue: "readonly",
  createHistogramSlider: "readonly",
};

// Symbols table-controls.js DEFINES and the table pages CONSUME (issue
// #188). Loaded after histogram-slider.js, which it instantiates.
const tableControlGlobals = {
  foldForSearch: "readonly",
  matchesSearch: "readonly",
  isRangeType: "readonly",
  isFilterUnset: "readonly",
  rowPassesFilter: "readonly",
  applyFilters: "readonly",
  rowsExceptFilter: "readonly",
  resolveVisibleColumns: "readonly",
  resolveFilters: "readonly",
  defaultFilterValues: "readonly",
  parseTableState: "readonly",
  serializeTableState: "readonly",
  // No `histogramBuckets`: it is table-controls.js's own, used only inside the
  // file and `require`d by the unit tests. Declaring it here said a page
  // script reads it, and none does.
  controlsHtml: "readonly",
  createTableControls: "readonly",
  syncSidebarDisclosure: "readonly",
  wireSidebarDisclosure: "readonly",
};

const browserRules = {
  "no-undef": "error",
  "no-unused-vars": ["error", { args: "none" }],
};

module.exports = [
  {
    // Don't lint installed deps or this config file itself.
    ignores: ["node_modules/**", "eslint.config.js"],
  },
  js.configs.recommended,
  {
    // The shared module: defines the sharedGlobals, so it must NOT list them
    // as globals. Its Node export shim references `module`.
    files: ["js/streetscape-utils.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser, ...vendorGlobals, module: "readonly" },
    },
    rules: browserRules,
  },
  {
    // Page scripts that consume the streetscape-utils.js globals.
    files: ["js/index.js", "js/city.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser, ...vendorGlobals, ...sharedGlobals },
    },
    rules: browserRules,
  },
  {
    // Street-coverage overlay (issue #24): consumes streetscape-utils.js
    // globals and defines renderStreetCoverage for city.js. Like
    // streetscape-utils.js, it carries a Node export shim (`module`).
    files: ["js/street-coverage.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser, ...vendorGlobals, ...sharedGlobals, module: "readonly" },
    },
    rules: browserRules,
  },
  {
    // Shared sortable-table machinery for the grid/streets table pages:
    // consumes streetscape-utils.js globals (coverageColor) and defines the
    // tableGlobals. Node export shim (`module`) for the unit tests.
    files: ["js/table-utils.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser, ...vendorGlobals, ...sharedGlobals, module: "readonly" },
    },
    rules: browserRules,
  },
  {
    // The histogram-slider filter control (issue #250): consumes
    // table-utils.js's formatCellNumber and defines the
    // histogramSliderGlobals. Node export shim (`module`) for the unit tests.
    files: ["js/histogram-slider.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser, ...vendorGlobals, ...tableGlobals, module: "readonly" },
    },
    rules: browserRules,
  },
  {
    // The exploration chassis (issue #188): consumes histogram-slider.js's
    // component and defines the tableControlGlobals. Node export shim
    // (`module`) for the unit tests.
    //
    // Deliberately NOT given tableGlobals: this file's last table-utils.js
    // dependency (formatCellNumber) went to histogram-slider.js, and `no-undef`
    // is the only thing enforcing the load order the docblock claims. Left
    // spread here, a new table-utils use would slip in silently — and
    // table-controls.js is loaded before table-utils.js on no page today, but
    // nothing but this list would notice if one changed.
    files: ["js/table-controls.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        ...globals.browser,
        ...vendorGlobals,
        ...sharedGlobals,
        ...histogramSliderGlobals,
        module: "readonly",
      },
    },
    rules: browserRules,
  },
  {
    // The three table pages (issues #99/#155, the grid table and the driving
    // join): consume the streetscape-utils.js, table-utils.js and
    // table-controls.js globals and, like the files above, carry a Node export
    // shim (`module`) so their pure helpers can be unit-tested.
    files: ["js/streets.js", "js/grid.js", "js/driving.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        ...globals.browser,
        ...vendorGlobals,
        ...sharedGlobals,
        ...tableGlobals,
        ...histogramSliderGlobals,
        ...tableControlGlobals,
        module: "readonly",
      },
    },
    rules: browserRules,
  },
  {
    // Diff overlay (change-since-previous-run dots): consumes
    // streetscape-utils.js globals and defines renderDiffOverlay for city.js.
    // Node export shim (`module`) for the unit tests.
    files: ["js/diff-overlay.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser, ...vendorGlobals, ...sharedGlobals, module: "readonly" },
    },
    rules: browserRules,
  },
  {
    // index.js carries a Node export shim (`module`) so its pure helpers can
    // be unit-tested (js/__tests__/index.test.js), like the page scripts
    // above. Flat config merges this into its entry.
    files: ["js/index.js"],
    languageOptions: {
      globals: { module: "readonly" },
    },
  },
  {
    // Only city.html loads street-coverage.js and diff-overlay.js, so only
    // city.js may consume their globals (flat config merges this into
    // city.js's entry above). The manifest helpers moved to
    // streetscape-utils.js (they are needed by index.js/streets.js too) and
    // are declared in sharedGlobals instead.
    files: ["js/city.js"],
    languageOptions: {
      globals: {
        renderStreetCoverage: "readonly",
        renderDiffOverlay: "readonly",
      },
    },
  },
  {
    // Offline unit tests, run under Node's built-in test runner (CommonJS).
    files: ["js/__tests__/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "commonjs",
      globals: { ...globals.node },
    },
  },
];
