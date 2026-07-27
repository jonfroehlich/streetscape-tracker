/**
 * streetscape-utils.js
 * Shared utilities for the Streetscape City Explorer.
 *
 * Provides the data-host base URL, the provider registry (GSV/Mapillary),
 * the YlOrRd color scale, filename→provider detection, gzip JSON fetching,
 * and the aggregate-record adapter — used by both the overview map
 * (index.js) and the per-city detail view (city.js).
 *
 * @module streetscape-utils
 */

/** Base URL for all Streetscape Tracker data files. */
const STREETSCAPE_DATA_BASE_URL =
  "https://makeabilitylab.cs.washington.edu/public/streetscape-tracker/data/";

/**
 * Max pano dots the city map draws at once (issues #77/#58). Dense cities hold
 * 10^5–10^6 panos; drawing one Leaflet marker layer each makes the map
 * unreadable at low zoom and freezes on every interaction. Above this count the
 * detail view draws a deterministic spatial subsample (spatialStrideSample) and
 * offers an opt-in "Render all" override. Reported stats/counts/plot always use
 * the FULL in-memory set — only the drawn dots are capped.
 */
const RENDER_CAP = 40000;

/**
 * Imagery provider registry. Each provider's color scale is anchored to
 * its launch date (oldest possible imagery = dark red), and each supplies
 * its own pano viewer deep-link and required attribution.
 */
const PROVIDERS = {
  gsv: {
    label: "Google Street View",
    panoNoun: "Google Panoramas",
    launchDate: new Date("2007-05-25"),
    attribution: "Panorama metadata © Google",
    viewerLabel: "View in Google Street View",
    viewerUrl: (panoId) =>
      `https://www.google.com/maps/@?api=1&map_action=pano&pano=${encodeURIComponent(panoId)}`,
  },
  mapillary: {
    label: "Mapillary",
    panoNoun: "Mapillary Panoramas",
    launchDate: new Date("2014-01-01"),
    attribution:
      'Image metadata © <a href="https://www.mapillary.com">Mapillary</a>, CC BY-SA',
    viewerLabel: "View in Mapillary",
    viewerUrl: (panoId) =>
      `https://www.mapillary.com/app/?pKey=${encodeURIComponent(panoId)}`,
  },
};

const MS_PER_YEAR = 1000 * 60 * 60 * 24 * 365.25;

/**
 * Maximum age (in years) mapped to the dark-red end of the color scale,
 * per provider. Computed once at load time so scales stay stable within
 * a session.
 */
const MAX_COLOR_AGE_BY_PROVIDER = Object.fromEntries(
  Object.entries(PROVIDERS).map(([key, p]) =>
    [key, (Date.now() - p.launchDate.getTime()) / MS_PER_YEAR])
);

/**
 * Return a CSS `rgb()` color for a given panorama age using a
 * three-stop YlOrRd interpolation (light yellow → orange → dark red),
 * scaled to the provider's imagery-age range.
 *
 * Stop 0 (age = 0):              rgb(255, 255, 178)  — light yellow
 * Stop 1 (age = max / 2):        rgb(253, 141,  60)  — orange
 * Stop 2 (age = provider max):   rgb(189,   0,  38)  — dark red
 *
 * @param {number} age - Panorama age in years (≥ 0).
 * @param {string} [provider="gsv"] - Provider key (see PROVIDERS).
 * @returns {string} CSS color string, e.g. `"rgb(253, 141, 60)"`.
 *
 * @example
 *   getColor(0);                 // "rgb(255, 255, 178)" — newest
 *   getColor(11, "mapillary");   // dark red — oldest possible Mapillary
 */
function getColor(age, provider = "gsv") {
  const maxAge = MAX_COLOR_AGE_BY_PROVIDER[provider] ?? MAX_COLOR_AGE_BY_PROVIDER.gsv;
  const ratio = Math.min(age / maxAge, 1);

  let r, g, b;
  if (ratio < 0.5) {
    const t = ratio * 2;
    r = 255 - t * (255 - 253);
    g = 255 - t * (255 - 141);
    b = 178 - t * (178 - 60);
  } else {
    const t = (ratio - 0.5) * 2;
    r = 253 - t * (253 - 189);
    g = 141 - t * 141;
    b = 60  - t * (60  - 38);
  }
  return `rgb(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)})`;
}

/**
 * Return a CSS `rgb()` color for a grid-coverage percentage using a
 * three-stop single-hue teal interpolation, dark → bright:
 *
 * Stop 0 (0% coverage):    rgb(21,  86,  97)   — dark teal (recessive)
 * Stop 1 (50% coverage):   rgb(69, 170, 176)   — mid teal
 * Stop 2 (100% coverage):  rgb(127, 244, 227)  — bright aqua
 *
 * On the dark basemap, well-covered cities glow the way imagery lines glow
 * in the detail view, while sparse cities recede (but stay ≥2:1 against the
 * basemap). Deliberately a different hue family from the YlOrRd age scale
 * (getColor) so the two "color by" modes are never confusable. Ramp
 * validated for monotone lightness and dark-surface contrast.
 *
 * @param {number} pct - Coverage percentage; clamped to [0, 100].
 * @returns {string} CSS color string, e.g. `"rgb(69, 170, 176)"`.
 *
 * @example
 *   coverageColor(0);    // "rgb(21, 86, 97)"  — no coverage
 *   coverageColor(100);  // "rgb(127, 244, 227)" — full grid coverage
 */
function coverageColor(pct) {
  const ratio = Math.min(Math.max(pct / 100, 0), 1);

  let r, g, b;
  if (ratio < 0.5) {
    const t = ratio * 2;
    r = 21 + t * (69 - 21);
    g = 86 + t * (170 - 86);
    b = 97 + t * (176 - 97);
  } else {
    const t = (ratio - 0.5) * 2;
    r = 69 + t * (127 - 69);
    g = 170 + t * (244 - 170);
    b = 176 + t * (227 - 176);
  }
  return `rgb(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)})`;
}

/**
 * "Color by" metric registry for the overview map. Each metric supplies
 * everything the UI needs to render one scalar per city consistently
 * across the map rectangles, legend buckets, and scatter plots — so adding
 * a metric (e.g. street coverage, #102) is one new entry here.
 *
 * Contract per metric:
 *   valueOf(city)  → the adapted city record's value, or null when absent
 *                    (null renders as the no-data gray and joins the
 *                    "No data" legend row)
 *   color(value, provider) → fill color for a value
 *   bucketOf(value) → integer legend-bucket id for a (non-null) value
 *   bucketLabel/bucketColor(bucket[, provider]) → legend row text/swatch
 *   legendBuckets(values) → bucket ids in display order (given the
 *                    non-null values present, for data-driven ranges)
 *   rangeLabel(minBucket, maxBucket) → filter-readout text for an inclusive
 *                    bucket range, e.g. "2–5 years" / "30–80%"
 *   sliderLabel    → metric noun for the range-slider thumbs' aria-labels,
 *                    e.g. "Minimum median age (years)"
 *   formatValue(value) → tooltip text, e.g. "4.2 years" / "51.2%"
 *   yMax           → scatter y-axis cap (null = auto)
 */
const METRICS = {
  age: {
    label: "Median age",
    legendTitle: "Median Age (years)",
    titleNoun: "Median Age",
    axisTitle: "Median Age (years)",
    yMax: null,
    valueOf: (city) => city.pano_age_stats?.median_pano_age_years ?? null,
    color: (value, provider) => getColor(value, provider),
    bucketOf: (value) => Math.floor(value),
    bucketLabel: (bucket) => `${bucket} year${bucket !== 1 ? "s" : ""}`,
    bucketColor: (bucket, provider) => getColor(bucket, provider),
    // 0..ceil(max median) ascending — newest (best) first, as before
    legendBuckets: (values) => {
      const maxYears = Math.ceil(Math.max(0, ...values));
      return Array.from({ length: maxYears + 1 }, (_, i) => i);
    },
    rangeLabel: (min, max) => min === max
      ? `${min} year${min !== 1 ? "s" : ""}`
      : `${min}–${max} years`,
    sliderLabel: "median age (years)",
    formatValue: (v) => `${v.toFixed(1)} years`,
  },
  coverage: {
    label: "Coverage",
    legendTitle: "Grid Coverage (%)",
    titleNoun: "Coverage %",
    axisTitle: "Grid Coverage (%)",
    yMax: 100,
    valueOf: (city) => city.coverage_rate_percent ?? null,
    // Coverage is cross-provider comparable (same frozen grid), so its
    // color scale is provider-independent — unlike the age scale, which
    // anchors to each provider's launch date.
    color: (value) => coverageColor(value),
    // Deciles: 0–10% … 90–100%; 100% folds into the top bucket (9)
    bucketOf: (value) => Math.min(Math.floor(value / 10), 9),
    bucketLabel: (bucket) => `${bucket * 10}–${bucket * 10 + 10}%`,
    bucketColor: (bucket) => coverageColor(bucket * 10 + 5),
    // 90–100% first — best-coverage top, mirroring newest-first for age
    legendBuckets: () => Array.from({ length: 10 }, (_, i) => 9 - i),
    // Decile bucket b spans [10b, 10b+10), so the upper edge is (max+1)*10
    rangeLabel: (min, max) => `${min * 10}–${(max + 1) * 10}%`,
    sliderLabel: "grid coverage (%)",
    formatValue: (v) => `${v.toFixed(1)}%`,
  },
};

// Any-imagery coverage (issue #116): Mapillary's full footprint including
// flat-only points, vs the 360°-only `coverage` metric. Identical color/bucket
// machinery — only the read differs — so it's derived from `coverage` rather
// than duplicated. For GSV (and pre-v7 data) the value falls back to the 360°
// rate, so the two views coincide there.
METRICS.coverage_any = {
  ...METRICS.coverage,
  label: "Any imagery",
  legendTitle: "Any-Imagery Coverage (%)",
  titleNoun: "Any-Imagery Coverage %",
  axisTitle: "Any-Imagery Coverage (%)",
  valueOf: (city) =>
    city.any_imagery_coverage_rate_percent ?? city.coverage_rate_percent ?? null,
  sliderLabel: "any-imagery coverage (%)",
};

// Road-walk street coverage (issue #99/#155). A DIFFERENT denominator from the
// two coverage metrics above: those are area-sampled (share of frozen grid
// points with imagery), this is the fraction of the city's OSM street network
// length actually driven. The value is not in cities.json.gz at all — it comes
// from the streetwalks.json.gz sidecar manifest and is attached to the city
// record by mergeStreetwalkStats(), so it is null for every city that has not
// been walked (which is most of them: collection is still a manual CLI).
// Same decile/color machinery as `coverage` — only the read differs.
METRICS.streets = {
  ...METRICS.coverage,
  label: "Street coverage",
  legendTitle: "Street Coverage (% of street-km)",
  titleNoun: "Street Coverage %",
  axisTitle: "Street Coverage (% of street-km)",
  valueOf: (city) => city.street_coverage_pct_by_length ?? null,
  sliderLabel: "street coverage (%)",
};

/**
 * True iff `key` is a real "color by" metric key ("age"/"coverage").
 * Object.hasOwn for the same reason as isKnownProvider: a URL-supplied
 * ?metric=constructor must never pass.
 *
 * @param {*} key - Candidate metric key (e.g. from a URL parameter).
 * @returns {boolean}
 */
function isKnownMetric(key) {
  return typeof key === "string" && Object.hasOwn(METRICS, key);
}

/**
 * Parse a URL-supplied `?filter=MIN-MAX` value (inclusive bucket ids for
 * the active metric) into a validated range. Like isKnownMetric, this
 * treats the input as hostile: anything malformed, inverted, or outside
 * [minBucket, maxBucket] is rejected rather than clamped, so a stale or
 * hand-edited URL can't produce a half-applied filter.
 *
 * @param {*} str - Raw parameter value, e.g. "2-5".
 * @param {number} minBucket - Lowest valid bucket id for the metric.
 * @param {number} maxBucket - Highest valid bucket id for the metric.
 * @returns {?{min: number, max: number}} The range, or null if invalid.
 */
function parseFilterParam(str, minBucket, maxBucket) {
  if (typeof str !== "string") return null;
  const match = /^(\d+)-(\d+)$/.exec(str);
  if (!match) return null;
  const min = parseInt(match[1], 10);
  const max = parseInt(match[2], 10);
  if (min > max || min < minBucket || max > maxBucket) return null;
  return { min, max };
}

/**
 * True iff `key` is a real provider key ("gsv"/"mapillary").
 *
 * Uses Object.hasOwn rather than a truthy `PROVIDERS[key]` lookup so
 * attacker-controlled strings that name Object.prototype members
 * (?provider=constructor) can never pass as a provider.
 *
 * @param {*} key - Candidate provider key (e.g. from a URL parameter).
 * @returns {boolean}
 */
function isKnownProvider(key) {
  return typeof key === "string" && Object.hasOwn(PROVIDERS, key);
}

/**
 * Derive the imagery provider from a run data filename (the JS mirror of
 * naming.py: an optional alphabetic token between the step size and the
 * run date; no token means GSV).
 *
 * @param {string} filename - e.g. "bend--or_width_5000_height_5000_step_20_mapillary_2026-07-05.csv.gz"
 * @returns {string} Provider key ("gsv" when no token present).
 */
function getProviderFromFilename(filename) {
  const m = /_step_\d+(?:\.\d+)?_([a-z]+)_\d{4}-\d{2}-\d{2}/.exec(filename || "");
  return m && isKnownProvider(m[1]) ? m[1] : "gsv";
}

/**
 * HTML-escape a string for safe interpolation into an HTML template.
 *
 * Every data-derived string that enters innerHTML / bindPopup /
 * bindTooltip markup MUST pass through this: copyright/photographer
 * fields are arbitrary third-party content (Mapillary contributor names,
 * archival GSV photographer credits), and city/state/country names come
 * from publicly editable OSM/Nominatim data.
 *
 * @param {*} value - Any value; non-strings are stringified ("" for null/undefined).
 * @returns {string} The escaped string.
 */
function escapeHtml(value) {
  if (value == null) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/**
 * Validate a run-data filename from an untrusted source (the ?file= URL
 * parameter) against the filename contract (the JS mirror of naming.py).
 *
 * Accepts every run-filename generation — legacy undated, buggy float
 * step, dated, provider-tagged — and rejects anything else, in particular
 * path separators / traversal, so a crafted ?file= can never fetch
 * resources outside the published data directory or non-run artifacts.
 *
 * @param {?string} filename - Candidate filename (no directories).
 * @returns {boolean} True iff it looks like a published run csv.gz.
 */
function isValidRunFilename(filename) {
  if (typeof filename !== "string") return false;
  return /^[^/\\?#]+_width_\d+_height_\d+_step_\d+(?:\.\d+)?(?:_[a-z]+)?(?:_\d{4}-\d{2}-\d{2})?\.csv\.gz$/
    .test(filename);
}

/**
 * Fetch a `.json.gz` file, decompress it with pako, and return the
 * parsed object.
 *
 * Note: pako is used here (rather than the native DecompressionStream)
 * because this function loads small metadata JSON files where the full
 * response is collected before parsing — the streaming pipeline in
 * city.js uses DecompressionStream for the large CSV files.
 *
 * @param {string} url - Full URL to the `.json.gz` resource.
 * @returns {Promise<Object>} The parsed JSON payload.
 * @throws {Error} On HTTP error or decompression/parse failure.
 *
 * @example
 *   const cities = await fetchGzippedJson(STREETSCAPE_DATA_BASE_URL + "cities.json.gz");
 */
async function fetchGzippedJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} fetching ${url}`);
  }
  const compressed = await response.arrayBuffer();
  const text = pako.inflate(new Uint8Array(compressed), { to: "string" });
  return JSON.parse(text);
}

// ---------------------------------------------------------------------------
// Streetwalk manifest (issue #155).
//
// `streetwalks.json.gz` is a sidecar index of the latest road-walk coverage
// artifact per (city, provider). It is deliberately NOT part of the
// cities.json.gz v3 aggregate (folding it in is #102), because a road-walk
// artifact is not a sibling of a grid run: its `sp{N}` spacing is a free
// parameter and its run date differs, so no consumer can derive its filename.
// All three pages read it: city.js to pick the artifact to render,
// index.js to color/annotate the overview, streets.js to list the walks.
// ---------------------------------------------------------------------------

/**
 * URL of the sidecar streetwalk manifest.
 * @returns {string}
 */
function streetwalkManifestUrl() {
  return STREETSCAPE_DATA_BASE_URL + "streetwalks.json.gz";
}

/**
 * Fetch the streetwalk manifest, or null when it's absent/unreadable (the
 * feature is optional — most deployments won't have one yet).
 * @returns {Promise<?Object>}
 */
async function fetchStreetwalkManifest() {
  try {
    return await fetchGzippedJson(streetwalkManifestUrl());
  } catch (e) {
    console.info("No streetwalk manifest (skipping road-walk overlay):", e.message);
    return null;
  }
}

/**
 * Find a city+provider's streetwalk entry in the manifest, or null.
 * @param {?Object} manifest - The parsed streetwalks.json.gz, or null.
 * @param {string} cityId
 * @param {string} provider
 * @returns {?Object} The walk record (with `coverage_filename`), or null.
 */
function lookupStreetwalk(manifest, cityId, provider) {
  if (!manifest || !Array.isArray(manifest.walks)) return null;
  return (
    manifest.walks.find((w) => w.city_id === cityId && w.provider === provider) || null
  );
}

/**
 * Attach each city's road-walk record from the manifest, in place.
 *
 * Adapted city records carry the canonical `city_id` and `provider`, which is
 * exactly the manifest's key — so this is a straight join. Cities without a
 * walk get explicit nulls rather than missing keys, so METRICS.streets.valueOf
 * and the popup both read a defined property.
 *
 * The count is the honest denominator for "N of M cities walked": today it is
 * 1 of ~1,150, and the overview says so rather than silently rendering a map
 * of grey rectangles.
 *
 * @param {Object[]} cities - Adapted city records (from adaptCitiesPayload).
 * @param {?Object} manifest - The parsed streetwalks.json.gz, or null.
 * @returns {number} How many cities matched a walk.
 *
 * @example
 *   const { cities } = adaptCitiesPayload(raw, "gsv");
 *   const walked = mergeStreetwalkStats(cities, manifest);  // → 1
 */
function mergeStreetwalkStats(cities, manifest) {
  let matched = 0;
  for (const city of cities) {
    const walk = lookupStreetwalk(manifest, city.city_id, city.provider);
    city.street_walk = walk;
    city.street_coverage_pct_by_length = walk?.coverage_pct_by_length ?? null;
    if (walk) matched += 1;
  }
  return matched;
}

/**
 * Flatten one aggregate city record into the flat shape the UI consumes.
 *
 * Handles all three aggregate generations:
 *   v1: already flat — passes through (gsv only)
 *   v2: {city_id, city, latest, runs, change}       (gsv only)
 *   v3: {city_id, city, providers: {gsv: {...}, mapillary: {...}}}
 *
 * Besides the historical flat fields, the result carries normalized
 * provider-agnostic keys the UI should prefer:
 *   provider        — the provider key this record was adapted for
 *   pano_count      — unique provider panos (google subset for gsv)
 *   pano_age_stats  — age stats of those panos
 *   capture_year_histogram — their year histogram ({counts} shape)
 *
 * @param {Object} rec - One entry of cities.json.gz `cities[]`.
 * @param {string} [provider="gsv"] - Which provider's view to adapt.
 * @returns {?Object} Flat record, or null when the city has no runs for
 *   the requested provider.
 */
function adaptCityRecord(rec, provider = "gsv") {
  if (!rec.latest && !rec.providers) {
    // schema v1: flat, gsv-only. A raw v1 record has the historical flat
    // fields but NOT the normalized keys (provider/pano_count/
    // pano_age_stats/capture_year_histogram), so derive them here rather
    // than passing the record through untouched — consumers read
    // pano_age_stats.median_pano_age_years unconditionally.
    if (provider !== "gsv") return null;
    const v1Counts = rec.panorama_counts || {};
    const v1Histograms = rec.histogram_of_capture_dates_by_year || {};
    return {
      ...rec,
      provider: "gsv",
      city_id: rec.city_id ?? null,
      runs: rec.runs || [],
      change: rec.change || null,
      latest_run_date: rec.latest_run_date ?? null,
      copyright_info_available: rec.copyright_info_available ?? true,
      // v1 is gsv-only; any-imagery coverage equals the 360° rate there.
      any_imagery_coverage_rate_percent: rec.coverage_rate_percent ?? null,
      num_flat_images: null,
      pano_count: v1Counts.unique_google_panos ?? v1Counts.unique_panos,
      pano_age_stats: rec.google_panos_age_stats ?? rec.all_panos_age_stats,
      capture_year_histogram: v1Histograms.google_panos ?? v1Histograms.all_panos,
    };
  }

  // v3 groups by provider; v2 is equivalent to a gsv-only providers map
  const block = rec.providers
    ? rec.providers[provider]
    : (provider === "gsv"
        ? { latest: rec.latest, runs: rec.runs, change: rec.change }
        : null);
  if (!block?.latest) return null;

  const latest = block.latest;
  const isGsv = provider === "gsv";
  const counts = latest.panorama_counts || {};
  const histograms = latest.histogram_of_capture_dates_by_year || {};

  return {
    provider,
    city_id: rec.city_id,
    city: rec.city.name,
    state: rec.city.state,
    country: rec.city.country,
    center: rec.city.center,
    bounds: rec.city.bounds,
    data_file: latest.data_file,
    json_file: latest.json_file,
    search_area_km2: latest.search_area_km2,
    coverage_rate_percent: latest.coverage_rate_percent,
    // Any-imagery (360° + flat) coverage, issue #116. Missing (GSV / pre-v7
    // runs) falls back to the 360° rate so the two views coincide there.
    any_imagery_coverage_rate_percent:
      latest.any_imagery_coverage_rate_percent ?? latest.coverage_rate_percent,
    num_flat_images: latest.num_flat_images ?? null,
    panorama_counts: counts,
    all_panos_age_stats: latest.all_panos_age_stats,
    google_panos_age_stats: latest.google_panos_age_stats,
    collection_info: latest.collection_info,
    histogram_of_capture_dates_by_year: histograms,
    latest_run_date: latest.run_date,
    runs: block.runs || [],
    change: block.change || null,
    // False for archival GSV runs that never captured copyright_info
    // (their Google subset is unknown; the fallbacks below kick in)
    copyright_info_available: latest.copyright_info_available ?? true,
    // Normalized provider-agnostic fields (prefer these in UI code)
    pano_count: isGsv ? (counts.unique_google_panos ?? counts.unique_panos)
                      : counts.unique_panos,
    pano_age_stats: isGsv ? (latest.google_panos_age_stats ?? latest.all_panos_age_stats)
                          : latest.all_panos_age_stats,
    capture_year_histogram: isGsv ? (histograms.google_panos ?? histograms.all_panos)
                                  : histograms.all_panos,
  };
}

/**
 * Adapt a whole cities.json.gz payload (v1, v2, or v3) to the flat-record
 * shape for one provider, returning {meta, cities}. Cities with no runs
 * for the provider are omitted.
 *
 * @param {Object} data - Parsed cities.json.gz payload.
 * @param {string} [provider="gsv"] - Which provider's view to adapt.
 * @returns {{meta: Object, cities: Object[]}}
 */
function adaptCitiesPayload(data, provider = "gsv") {
  if (!data?.cities || !Array.isArray(data.cities)) {
    throw new Error("Invalid data format: missing cities array");
  }
  return {
    meta: {
      citiesCount: data.cities_count,
      generatedAt: data.generated_at || data.creation_timestamp,
      schemaVersion: data.schema_version || 1,
    },
    cities: data.cities
      .map((rec) => adaptCityRecord(rec, provider))
      .filter(Boolean),
  };
}

// ---------------------------------------------------------------------------
// Pure display/derivation helpers.
//
// These carry the numeric/date edge cases that produced the B1–B4 tooltip
// bugs (Infinity%/NaN) and the 0-pano epoch-date bug (#122/#69). They are
// deliberately DOM-free and side-effect-free so index.js/city.js can share
// them and the Node unit tests (issue #123) can exercise them directly.
// ---------------------------------------------------------------------------

/**
 * Exact "official Google" copyright test — the JS mirror of
 * analysis.is_google_copyright. Matches ONLY the literal `© Google`
 * string, never a substring, because third-party photographer names can
 * themselves contain "Google" (e.g. "Google Street View contributor").
 *
 * @param {?string} copyright - A run row's copyright_info field.
 * @returns {boolean} True iff the imagery is official Google.
 */
function isGoogleCopyright(copyright) {
  return copyright === "© Google";
}

/**
 * Parse a pano capture date, returning null when the date is absent
 * (age_stats are all null for a 0-pano run). Guards against
 * `new Date(null)` silently rendering as the Unix epoch (12/31/1969)
 * instead of a "—"/"No data" placeholder (issue #122, #69 family).
 *
 * Date-ONLY strings ("YYYY-MM-DD") are parsed as LOCAL midnight, not UTC:
 * `new Date("2023-01-01")` is UTC midnight, and reading it back with local
 * getters (toLocaleDateString, getFullYear) west of UTC shifts every date
 * back a day — and every January/year-precision capture date back a whole
 * YEAR (standardize_capture_date pins month/year precision to the 1st), so
 * US visitors saw those panos in the previous year's filter bucket and
 * color. Full timestamps (with a time component) still parse natively.
 *
 * @param {?string} v - ISO date string, or null/undefined.
 * @returns {?Date} A Date (local midnight for date-only), or null when falsy.
 */
function panoDateOrNull(v) {
  if (!v) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(v));
  if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return new Date(v);
}

/**
 * Official-Google share of all found panoramas, as a fixed-1-decimal
 * percent string. Guards the divide-by-zero on a 0-pano run that would
 * otherwise render "Infinity%" in the overview tooltip (B1–B4 audit).
 *
 * @param {number} googleCount - Unique official-Google panos.
 * @param {number} totalCount - Unique panos of all copyrights.
 * @returns {string} e.g. "37.2"; "0.0" when totalCount <= 0.
 */
function googleSharePercent(googleCount, totalCount) {
  if (!(totalCount > 0)) return "0.0";
  return ((googleCount / totalCount) * 100).toFixed(1);
}

/**
 * Build a gap-filled year→count histogram from a sparse capture-year map,
 * spanning the earliest present year through currentYear (inclusive), with
 * missing interior years zero-filled. Returns {} when there are no years,
 * avoiding the `Math.min(...[]) === Infinity` blow-up on an empty or
 * missing histogram (issue #69).
 *
 * @param {?Object<string|number, number>} rawHistogram - Sparse year→count.
 * @param {number} currentYear - Upper bound (inclusive) of the fill range.
 * @returns {Object<number, number>} Dense year→count, or {} when empty.
 */
function buildFilledHistogram(rawHistogram, currentYear) {
  const source = rawHistogram || {};
  const years = Object.keys(source).map(Number);
  const filled = {};
  if (years.length > 0) {
    const startYear = Math.min(...years);
    for (let y = startYear; y <= currentYear; y++) {
      filled[y] = source[y] || 0;
    }
  }
  return filled;
}

/**
 * Add (or replace) an alpha channel on a CSS `rgb()`/`rgba()` color.
 * Non-rgb inputs (hex, named colors) are returned unchanged.
 *
 * @param {string} color - e.g. "rgb(253, 141, 60)".
 * @param {number} alpha - 0..1.
 * @returns {string} e.g. "rgba(253, 141, 60, 0.3)".
 */
function withAlpha(color, alpha) {
  const m = /^rgba?\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)/.exec(color || "");
  return m ? `rgba(${m[1]}, ${m[2]}, ${m[3]}, ${alpha})` : color;
}

/**
 * Format an age in years for display: "4.2 years", or an em dash when the
 * value is absent (null age stats on a 0-pano run).
 *
 * @param {?number} v - Age in years.
 * @returns {string}
 */
function fmtYears(v) {
  return v != null ? `${v.toFixed(1)} years` : "—";
}

/**
 * Pre-format the display strings for a change_from_previous_run block so
 * index.js (overview popup) and city.js (legend) render identical numbers
 * and only differ in markup. Returns null when there is no change block.
 *
 * @param {?Object} change - {panos_added, panos_removed,
 *   capture_date_changed, coverage_delta_pct, from/from_run_date}.
 * @returns {?{from: string, added: string, removed: string,
 *   redated: ?string, coverage: ?string}}
 */
function formatChangeSummary(change) {
  if (!change) return null;
  return {
    from: change.from ?? change.from_run_date ?? "",
    added: `+${(change.panos_added ?? 0).toLocaleString()} new`,
    removed: `−${(change.panos_removed ?? 0).toLocaleString()} removed`,
    redated: change.capture_date_changed
      ? `${change.capture_date_changed.toLocaleString()} panos re-dated`
      : null,
    coverage: change.coverage_delta_pct != null
      ? `${change.coverage_delta_pct >= 0 ? "+" : ""}${change.coverage_delta_pct.toFixed(2)} pct points`
      : null,
  };
}

/**
 * Deterministic spatial-stride subsample used by the city map's render cap
 * (issues #77/#58). Orders the points spatially (by latitude, then longitude)
 * and takes an even stride so the drawn subset stays spread across the city —
 * density and coverage gaps remain honest, unlike a random or head-of-list
 * drop that would clump in whatever order the CSV happened to arrive.
 *
 * Pure and Leaflet-free so it is node-testable (mirrors METRICS/adaptCityRecord).
 *
 * @param {Array<[number, number]>} points - [lat, lon] pairs (index-aligned to
 *   the caller's marker array).
 * @param {number} cap - Maximum indices to return.
 * @returns {number[]} Indices INTO `points`: all of them when `points.length <=
 *   cap` (or the input is empty), otherwise exactly `cap` spatially-strided
 *   indices. Returns `[]` when `cap <= 0` or is not finite.
 */
function spatialStrideSample(points, cap) {
  const n = points.length;
  if (!Number.isFinite(cap) || cap <= 0) return [];
  if (n <= cap) return points.map((_, i) => i);

  // Order indices spatially so an even stride samples across the whole extent.
  const order = points.map((_, i) => i).sort((a, b) => {
    const pa = points[a];
    const pb = points[b];
    return pa[0] - pb[0] || pa[1] - pb[1];
  });

  const stride = n / cap;
  const out = [];
  for (let k = 0; k < cap; k++) {
    out.push(order[Math.floor(k * stride)]);
  }
  return out;
}

/**
 * Diff the currently-drawn marker set against a desired target set, returning
 * exactly which markers to add and remove. Lets every map interaction touch
 * only the delta instead of re-adding/removing all N markers (the O(N)-per-click
 * churn behind the forced-reflow jank in #58). Compares by object identity, so
 * the same marker instances must be shared across the caller's caches.
 *
 * @param {Set} onMapSet - Markers currently on the map.
 * @param {Iterable} target - Desired drawn set (Set or array).
 * @returns {{toAdd: Array, toRemove: Array}}
 */
function computeVisibilityDelta(onMapSet, target) {
  const targetSet = target instanceof Set ? target : new Set(target);
  const toAdd = [];
  const toRemove = [];
  for (const m of targetSet) if (!onMapSet.has(m)) toAdd.push(m);
  for (const m of onMapSet) if (!targetSet.has(m)) toRemove.push(m);
  return { toAdd, toRemove };
}

/**
 * Leaflet circle style for a pano marker under the temporal date filter. When a
 * date is selected, matching panos are emphasized and the rest dimmed; with no
 * date, every marker gets the default style. Kept pure (returns a plain style
 * object, no Leaflet) so the date-filter styling is node-testable; the caller
 * maps date values to comparable strings (both sides use the same convention).
 *
 * @param {?string} captureDateStr - The marker's capture-date key.
 * @param {?string} selectedDateStr - The selected date key, or null/"" when no
 *   date filter is active.
 * @returns {{fillOpacity: number, radius: number}}
 */
function markerDateStyle(captureDateStr, selectedDateStr) {
  if (!selectedDateStr) return { fillOpacity: 0.8, radius: 3 };
  return captureDateStr === selectedDateStr
    ? { fillOpacity: 1, radius: 4 }
    : { fillOpacity: 0.05, radius: 3 };
}

// Node/CommonJS export shim for the unit tests (issue #123). This is a no-op
// in the browser, where these symbols are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    STREETSCAPE_DATA_BASE_URL,
    RENDER_CAP,
    PROVIDERS,
    METRICS,
    isKnownProvider,
    isKnownMetric,
    parseFilterParam,
    getColor,
    coverageColor,
    escapeHtml,
    isValidRunFilename,
    getProviderFromFilename,
    fetchGzippedJson,
    streetwalkManifestUrl,
    fetchStreetwalkManifest,
    lookupStreetwalk,
    mergeStreetwalkStats,
    adaptCityRecord,
    adaptCitiesPayload,
    isGoogleCopyright,
    panoDateOrNull,
    googleSharePercent,
    buildFilledHistogram,
    withAlpha,
    fmtYears,
    formatChangeSummary,
    spatialStrideSample,
    computeVisibilityDelta,
    markerDateStyle,
  };
}
