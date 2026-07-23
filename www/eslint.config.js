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
  RENDER_CAP: "readonly",
  PROVIDERS: "readonly",
  METRICS: "readonly",
  isKnownProvider: "readonly",
  isKnownMetric: "readonly",
  parseFilterParam: "readonly",
  getColor: "readonly",
  coverageColor: "readonly",
  escapeHtml: "readonly",
  isValidRunFilename: "readonly",
  getProviderFromFilename: "readonly",
  fetchGzippedJson: "readonly",
  adaptCityRecord: "readonly",
  adaptCitiesPayload: "readonly",
  isGoogleCopyright: "readonly",
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
    // Only city.html loads street-coverage.js, so only city.js may consume
    // its globals (flat config merges this into city.js's entry above).
    files: ["js/city.js"],
    languageOptions: {
      globals: {
        renderStreetCoverage: "readonly",
        fetchStreetwalkManifest: "readonly",
        lookupStreetwalk: "readonly",
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
