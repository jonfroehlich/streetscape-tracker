/**
 * streetscape-utils.js
 * Shared utilities for the Streetscape City Explorer.
 *
 * Provides the data-host base URL, the shared dark basemap layer, the
 * provider registry (GSV/Mapillary), the YlOrRd color scale,
 * filename→provider detection, gzip JSON fetching, and the aggregate-record
 * adapter — used by both the overview map (index.js) and the per-city
 * detail view (city.js).
 *
 * @module streetscape-utils
 */

/** Base URL for all Streetscape Tracker data files. */
const STREETSCAPE_DATA_BASE_URL =
  "https://makeabilitylab.cs.washington.edu/public/streetscape-tracker/data/";

/**
 * CARTO basemap API key — public on purpose, not a secret.
 *
 * CARTO now requires a key on basemaps.cartocdn.com (observed 2026-08-28).
 * A keyless request still returns HTTP 200: the tile PNG itself comes back
 * stamped "API KEY REQUIRED", so the break renders as a styling bug rather
 * than an outage, and no amount of error handling on our side can see it.
 *
 * The token ships in static JS because it has to: the browser sends it to
 * CARTO on every tile request, so it is readable off the deployed page no
 * matter where the repo keeps it, and the site has no build step to hide it
 * behind (ADR 0001). CARTO asks for a domain when issuing the key but does
 * NOT enforce it — a tile request with a mismatched Referer and one with no
 * Referer at all both returned the same keyed bytes (checked 2026-08-28) —
 * so treat this as bearer-style. Rotate by replying to the issuing email; the
 * key lives in this one const so that stays a one-line change.
 *
 * The free ceiling is 5M tile requests per calendar month, counted across the
 * raster AND vector services, conditioned on crediting CARTO and
 * OpenStreetMap — which is why addBasemapLayer sets the attribution.
 *
 * Raster PNG is on CARTO's stated retirement path, but vector will require
 * this same key eventually (CARTO: coming, not live yet, as of 2026-08-28).
 * So switching to vector is a rendering-stack decision — Leaflet cannot draw
 * MVT, and maplibre-gl is ~273 KB gzipped against Leaflet's ~42 KB — and not
 * a way off the key.
 *
 * @see https://docs.carto.com/faqs/carto-basemaps
 */
const CARTO_BASEMAP_KEY = "cb1_2g44_1_b50596cc0f87c5ea43d9b94b";

/**
 * Add the shared dark basemap to a Leaflet map.
 *
 * The overview map (index.js) and the per-city detail map (city.js) draw the
 * same tiles. They call this instead of each repeating the URL so the key and
 * the required attribution have exactly one place to be updated — two copies
 * of a key that must match is how one map silently keeps the watermark.
 *
 * @param {L.Map} map - Map to add the basemap to.
 * @returns {Object} The tile layer, already added to the map.
 */
function addBasemapLayer(map) {
  return L.tileLayer(
    `https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png?key=${CARTO_BASEMAP_KEY}`,
    {
      attribution: "© OpenStreetMap contributors © CARTO",
      maxZoom: 19
    }
  ).addTo(map);
}

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
 *
 * `earliestPlausibleCapture` is a SEPARATE date from `launchDate` and must
 * stay that way: the launch date anchors the color ramp, while this is the
 * floor below which a capture date cannot be true (issue #213). It mirrors
 * analysis.EARLIEST_PLAUSIBLE_CAPTURE — Mapillary's is far earlier than its
 * founding because contributors upload genuinely old photographs.
 *
 * It is built with the LOCAL-midnight constructor, not `new Date("2007-01-01")`,
 * because it is compared against `panoDateOrNull` output, which parses a
 * date-only string as local midnight (deliberately — see that function). Mixing
 * the two makes the floor exclusive east of UTC and inclusive west of it, so a
 * pano captured exactly on the floor would be kept in every published artifact
 * (analysis.plausible_capture_mask uses an inclusive `between`) but dropped
 * from the map for some visitors. `launchDate` stays an ISO literal: it only
 * anchors a color ramp, where a one-day shift is invisible.
 *
 * The `has*` entries are CAPABILITIES, not identity. Every UI branch that used
 * to ask `provider === "gsv"` asks one of them instead (issue #225), so adding
 * a third provider is a registry edit rather than a hunt through the `=== "gsv"`
 * comparisons that were scattered over three files:
 *   hasCopyrightFilter — publishes a copyright field, so its imagery splits
 *     into an official-fleet subset and contributor uploads (GSV's `© Google`).
 *   hasFlatImagery — publishes flat/perspective imagery alongside 360°
 *     panoramas, so a coverage number has to say which of the two it counts.
 *
 * `description` is the one-line "what is this provider's data" text; index.js
 * renders it as the provider toggle's tooltip, so a newly registered provider
 * arrives in that radio group with its own explanation rather than a blank.
 *
 * `viewerUrl(panoId, row)` takes the whole CSV row, not just the image key,
 * because not every provider addresses an image by its own id: KartaView's
 * viewer is keyed on (sequence_id, sequence_index) and cannot build a link from
 * the photo id at all. GSV and Mapillary ignore the second argument. It may
 * return null — meaning "this row is not addressable" — and the popup builders
 * render that as no link rather than a dead one.
 */
const PROVIDERS = {
  gsv: {
    label: "Google Street View",
    // Column-header form (issue #250). The pivoted grid/streets tables put one
    // sub-column PER PROVIDER under a grouped header, so the leaf label is
    // repeated across every metric group and has to be short enough that three
    // of them fit a measure. Consumers fall back to `label`, so a provider
    // registered without one still renders.
    shortLabel: "GSV",
    // How a provider's raw pano COUNT is arrived at — the reason those counts
    // are not comparable across providers even though the coverage
    // percentages are. Rendered as the parenthetical on the pivot's
    // "Panoramas" leaves, which is the one place the two numbers sit side by
    // side and most invite being subtracted.
    panoCountingModel: "sample",
    description: "Official Google Street View metadata, sampled at each grid point",
    panoNoun: "Google Panoramas",
    launchDate: new Date("2007-05-25"),
    earliestPlausibleCapture: new Date(2007, 0, 1), // local midnight; see above
    attribution: "Panorama metadata © Google",
    viewerLabel: "View in Google Street View",
    viewerUrl: (panoId) =>
      `https://www.google.com/maps/@?api=1&map_action=pano&pano=${encodeURIComponent(panoId)}`,
    hasCopyrightFilter: true,
    hasFlatImagery: false,
  },
  mapillary: {
    label: "Mapillary",
    shortLabel: "Mapillary",
    panoCountingModel: "census",
    description: "Crowdsourced Mapillary imagery: a census of every 360° panorama",
    panoNoun: "Mapillary Panoramas",
    launchDate: new Date("2014-01-01"),
    earliestPlausibleCapture: new Date(2004, 0, 1), // local midnight; see above
    attribution:
      'Image metadata © <a href="https://www.mapillary.com">Mapillary</a>, CC BY-SA',
    viewerLabel: "View in Mapillary",
    viewerUrl: (panoId) =>
      `https://www.mapillary.com/app/?pKey=${encodeURIComponent(panoId)}`,
    hasCopyrightFilter: false,
    hasFlatImagery: true,
  },
  kartaview: {
    label: "KartaView",
    description:
      "Crowdsourced KartaView imagery: a census of every 360° panorama, mostly Grab fleet capture",
    panoNoun: "KartaView Panoramas",
    // KartaView launched as OpenStreetView in 2016. The ramp anchor only.
    launchDate: new Date("2016-01-01"),
    // 2004, mirroring analysis.EARLIEST_PLAUSIBLE_CAPTURE["kartaview"] and
    // deliberately looser than the 2016 launch for the same reason Mapillary's
    // is looser than 2013: the imagery is community dashcam footage and
    // contributors upload genuinely old photographs. Ties Mapillary's floor, so
    // LOOSEST_EARLIEST_PLAUSIBLE_CAPTURE is unmoved and the JS/Python
    // divergence documented above stays dormant.
    earliestPlausibleCapture: new Date(2004, 0, 1), // local midnight; see above
    attribution:
      'Image metadata © <a href="https://kartaview.org">KartaView</a>, CC BY-SA',
    viewerLabel: "View in KartaView",
    // The ONLY entry that needs the row, and the reason viewerUrl takes one.
    // KartaView's viewer is addressed by (sequence, index within it) rather
    // than by photo id -- verified against a real photo from the API
    // (id 2627370567 lives at sequence 11616154 index 1) -- so `panoId` alone
    // cannot build this link. Both fields are in the run schema precisely
    // because the bulk sweep row already carries them.
    //
    // Returns null when either is missing, which the popup builders render as
    // no link at all. A KartaView row can legitimately lack a sequence (the
    // decoder keeps a null rather than inventing one), and a link to nowhere
    // is worse than none -- the same call the FLAT_ONLY popup already makes.
    viewerUrl: (panoId, row) => {
      const sequence = row?.sequence_id;
      const index = row?.sequence_index;
      if (!sequence || index === null || index === undefined || index === "") return null;
      return (
        `https://kartaview.org/details/${encodeURIComponent(sequence)}` +
        `/${encodeURIComponent(index)}`
      );
    },
    hasCopyrightFilter: false,
    // Overwhelmingly flat dashcam imagery outside the Grab fleet markets, so a
    // coverage number has to say which of the two it counts.
    hasFlatImagery: true,
  },
};

/**
 * Fallback floor for a provider that is not in the registry.
 *
 * Deliberately the LOOSEST floor any provider declares, computed rather than
 * spelled as one provider's name: `?? PROVIDERS.mapillary.earliestPlausibleCapture`
 * reads as "Mapillary is special" when what is meant is "when in doubt, drop
 * as little as possible". A too-tight fallback silently deletes real imagery
 * from the map; a too-loose one shows a handful of bad dates, which is the
 * cheaper error.
 *
 * This DIVERGES from the Python side on purpose, and the divergence is worth
 * knowing about rather than papering over: analysis._DEFAULT_EARLIEST_PLAUSIBLE_CAPTURE
 * pins the same fallback to a NAMED provider (`EARLIEST_PLAUSIBLE_CAPTURE["mapillary"]`),
 * which is a min only by coincidence. Both read 2004-01-01 today, so nothing
 * observable differs. They part company the day a provider is registered with
 * a floor earlier than Mapillary's — this one would loosen automatically and
 * Python's would not, and the map would then draw a pano the published stats
 * dropped. Whoever registers that provider owns the choice: give Python the
 * same min, or tighten this to match whatever Python names.
 */
const LOOSEST_EARLIEST_PLAUSIBLE_CAPTURE = new Date(
  Math.min(...Object.values(PROVIDERS).map((p) => p.earliestPlausibleCapture.getTime()))
);

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

// Any-imagery coverage (issue #116): a provider's full footprint including
// flat-only points, vs the 360°-only `coverage` metric. Identical color/bucket
// machinery — only the read differs — so it's derived from `coverage` rather
// than duplicated. For a provider that publishes no flat imagery (hasFlatImagery
// false — GSV today) and for pre-v7 data the value falls back to the 360° rate,
// so the two views coincide there.
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
// been walked yet — the street channels are scheduled like the grid ones, so
// cities fill in over a collection cycle rather than all at once.
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

// Data freshness (how recently each city was collected). The distribution is
// strongly bimodal — a large 2025-01/02 initial-collection cohort plus a small
// continuously-refreshed tail — so fixed categorical recency buckets read far
// better than linear time buckets (which would put ~everything in one bar).
const MS_PER_MONTH = MS_PER_YEAR / 12;

/** Freshness buckets, freshest first; index = bucket id (non-negative, so
 *  parseFilterParam and the legend slider work unchanged). */
const FRESHNESS_BUCKETS = [
  { label: "Last 3 months", maxMonths: 3 },
  { label: "3–6 months", maxMonths: 6 },
  { label: "6–12 months", maxMonths: 12 },
  { label: "1–1.5 years", maxMonths: 18 },
  { label: "Over 1.5 years", maxMonths: Infinity },
];

/**
 * Color for a freshness bucket: a violet sequential ramp, bright (just
 * collected — glows on the dark basemap) → dark (stale — recedes), monotone
 * in lightness. Deliberately a third hue family, so age (YlOrRd), coverage
 * (teal), and freshness are never confusable.
 *
 * @param {number} bucket - FRESHNESS_BUCKETS index (clamped).
 * @returns {string} CSS hex color.
 */
function recencyColor(bucket) {
  const colors = ["#ecdcff", "#c19cf0", "#9666d1", "#6d3fa6", "#452a69"];
  return colors[Math.min(Math.max(bucket, 0), colors.length - 1)];
}

METRICS.freshness = {
  label: "Freshness",
  legendTitle: "Data Freshness (last collected)",
  titleNoun: "Data Age",
  axisTitle: "Months since collection",
  yMax: null,
  // Months since the latest run. panoDateOrNull parses the date-only run
  // date at LOCAL midnight (the UTC-shift guard all date reads share).
  valueOf: (city) => {
    const d = panoDateOrNull(city.latest_run_date);
    return d ? (Date.now() - d.getTime()) / MS_PER_MONTH : null;
  },
  color: (value) => recencyColor(METRICS.freshness.bucketOf(value)),
  bucketOf: (months) => FRESHNESS_BUCKETS.findIndex((b) => months <= b.maxMonths),
  bucketLabel: (bucket) => FRESHNESS_BUCKETS[bucket].label,
  bucketColor: (bucket) => recencyColor(bucket),
  // Fixed bucket set, freshest first — unlike age's data-driven range
  legendBuckets: () => FRESHNESS_BUCKETS.map((_, i) => i),
  rangeLabel: (min, max) => min === max
    ? FRESHNESS_BUCKETS[min].label
    : `${FRESHNESS_BUCKETS[min].label} – ${FRESHNESS_BUCKETS[max].label.toLowerCase()}`,
  sliderLabel: "data age",
  formatValue: (v) => (v < 1 ? "collected this month" : `collected ${v.toFixed(1)} months ago`),
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
 * Build a run-diff detail filename — the JS mirror of
 * diff.generate_diff_filename (diff.py): GSV keeps the tokenless form, other
 * providers carry their token between "diff" and the date pair.
 *
 * @param {string} cityId - Canonical city_id (may contain dots, e.g. "st.-louis--mo").
 * @param {string} provider - Provider key ("gsv" emits no token).
 * @param {string} fromDate - "YYYY-MM-DD" of the earlier run.
 * @param {string} toDate - "YYYY-MM-DD" of the later run.
 * @returns {string} e.g. "bend--or_diff_2026-04-01_to_2026-07-01.csv.gz"
 */
function diffFilenameFor(cityId, provider, fromDate, toDate) {
  const token = provider === "gsv" ? "" : `${provider}_`;
  return `${cityId}_diff_${token}${fromDate}_to_${toDate}.csv.gz`;
}

/**
 * Strict validator for a diff detail filename, applied before ANY fetch —
 * whether the name came from a run's JSON (`change_from_previous_run
 * .diff_file`) or was constructed by diffFilenameFor. Mirrors
 * isValidRunFilename's hostile-input stance (no path separators/traversal/
 * query chars), and the two contracts stay disjoint: isValidRunFilename
 * continues to REJECT diff names for `?file=`, and this rejects run names —
 * a poisoned aggregate/stats payload cannot turn the page into a fetch proxy
 * beyond the published data directory.
 *
 * @param {?string} filename - Candidate filename (no directories).
 * @returns {boolean} True iff it looks like a published diff detail csv.gz.
 */
function isValidDiffFilename(filename) {
  if (typeof filename !== "string") return false;
  return /^[^/\\?#]+_diff_(?:[a-z]+_)?\d{4}-\d{2}-\d{2}_to_\d{4}-\d{2}-\d{2}\.csv\.gz$/
    .test(filename);
}

/**
 * Fetch a small `.csv.gz` file, decompress it with pako, and return the text.
 * Diff detail files hold only the churn between two runs, so the streaming
 * pipeline city.js uses for full run CSVs is unnecessary here.
 *
 * @param {string} url - Full URL to the `.csv.gz` resource.
 * @returns {Promise<string>} The decompressed text.
 * @throws {Error} On HTTP error or decompression failure.
 */
async function fetchGzippedText(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} fetching ${url}`);
  }
  const compressed = await response.arrayBuffer();
  return pako.inflate(new Uint8Array(compressed), { to: "string" });
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
 * The OSM network a road walk covers when none is stated. 'drive' is the
 * original and still-scheduled series (motorized public roads only); a broad
 * 'all_public' walk additionally covers alleys, footways, park paths, cycleways
 * and steps, and is a SEPARATE series with a much larger street-km denominator.
 */
const DEFAULT_STREET_NETWORK_TYPE = "drive";

/**
 * Human labels for the osmnx network types a walk can be collected on. Keyed by
 * the exact `network_type` the manifest carries, so the keys double as the set
 * of values `?network=` accepts. Labels say what the walk COVERS rather than
 * echoing the osmnx filter name, since that distinction is the whole reason two
 * rows for one city are not duplicates.
 *
 * Mirrors naming.STREETWALK_NETWORK_TOKENS on the Python side — keep in sync.
 */
const STREET_NETWORK_LABELS = {
  drive: "Roads",
  all_public: "Roads + paths",
  all: "Roads + paths (incl. private)",
  walk: "Walkable",
  bike: "Bikeable",
  drive_service: "Roads + service",
};

/**
 * Human label for an OSM network type; unknown types render as themselves.
 * @param {?string} networkType - From the manifest; absent on pre-network walks.
 * @returns {string}
 */
function streetNetworkLabel(networkType) {
  const key = networkType ?? DEFAULT_STREET_NETWORK_TYPE;
  return STREET_NETWORK_LABELS[key] ?? key;
}

/**
 * Whether a string names a network type this site knows how to select on.
 * Used to validate the untrusted `?network=` parameter before it reaches a
 * manifest lookup, the same way isKnownProvider guards `?provider=`.
 * @param {?string} networkType
 * @returns {boolean}
 */
function isKnownStreetNetworkType(networkType) {
  return (
    typeof networkType === "string" &&
    Object.prototype.hasOwnProperty.call(STREET_NETWORK_LABELS, networkType)
  );
}

/**
 * Find a city+provider+network's streetwalk entry in the manifest, or null.
 *
 * A city can have one walk per network type, so this must select on network
 * type rather than take the first match — otherwise a city with both a drive
 * and an all_public walk would render whichever the manifest happened to list
 * first, and its headline coverage % would silently switch denominators.
 * Manifest entries written before network types existed have no `network_type`
 * field; they are drive walks, hence the `?? DEFAULT` on the record side.
 *
 * @param {?Object} manifest - The parsed streetwalks.json.gz, or null.
 * @param {string} cityId
 * @param {string} provider
 * @param {string} [networkType] - Defaults to "drive".
 * @returns {?Object} The walk record (with `coverage_filename`), or null.
 */
function lookupStreetwalk(
  manifest,
  cityId,
  provider,
  networkType = DEFAULT_STREET_NETWORK_TYPE,
) {
  if (!manifest || !Array.isArray(manifest.walks)) return null;
  return (
    manifest.walks.find(
      (w) =>
        w.city_id === cityId &&
        w.provider === provider &&
        (w.network_type ?? DEFAULT_STREET_NETWORK_TYPE) === networkType,
    ) || null
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
 * Indexes the walks once instead of scanning them per city: street collection
 * is scheduled across the whole catalog, so both sides of this join grow to
 * ~1,150 cities × 2 providers, and it re-runs on every provider/metric toggle.
 * The index keeps the FIRST walk per key, matching what `lookupStreetwalk`'s
 * `find` returns for a manifest that somehow carries duplicates.
 *
 * Only DRIVE walks are joined. METRICS.streets compares cities to each other,
 * and drive vs all_public coverage are not comparable numbers — they divide by
 * different street-km denominators — so mixing them would put two scales in one
 * choropleth. A city with only a broad walk therefore reads "No data" here,
 * which is honest: its drive coverage genuinely has not been measured.
 *
 * The count is the honest denominator for "N of M cities walked" that the
 * overview banner reports, rather than silently rendering a map of grey
 * rectangles.
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
  const byKey = new Map();
  if (manifest && Array.isArray(manifest.walks)) {
    for (const walk of manifest.walks) {
      if ((walk.network_type ?? DEFAULT_STREET_NETWORK_TYPE) !== DEFAULT_STREET_NETWORK_TYPE) {
        continue;
      }
      const key = `${walk.provider}|${walk.city_id}`;
      if (!byKey.has(key)) byKey.set(key, walk);
    }
  }

  let matched = 0;
  for (const city of cities) {
    const walk = byKey.get(`${city.provider}|${city.city_id}`) ?? null;
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
      // v1 predates the grid-size keys and will never gain them; null so
      // consumers read one shape rather than distinguishing null from
      // undefined.
      total_search_points: null,
      grid: null,
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
  // All three normalizations below ask ONE question -- does this provider
  // publish a copyright-filtered subset of its own imagery? -- so they read it
  // off the registry rather than each testing for "gsv". A third provider
  // hitting `isGsv ? ... : ...` silently inherited Mapillary's shape, which
  // happens to be right for a census provider and would be wrong for the first
  // one that isn't.
  const copyrightFiltered = PROVIDERS[provider]?.hasCopyrightFilter ?? false;
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
    // Grid size in sample points, and the geometry behind it. Null on records
    // published before the aggregate carried them (and on v1/v2 records, which
    // never will) — the denominator of coverage_rate_percent, so the tables can
    // show what a percentage is a percentage OF.
    //
    // `grid` is either null or fully populated, never an object of nulls: the
    // aggregate indexes the three keys rather than guarding them, so
    // `if (rec.grid)` is a sound test. It describes the LATEST RUN's grid, not
    // the city's current frozen geometry — the two diverge for cities resized
    // catalog-only by cap_oversized_grids.py (#166) until their next
    // collection, so label it as the run's grid rather than the city's.
    total_search_points: latest.total_search_points ?? null,
    grid: latest.grid ?? null,
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
    pano_count: copyrightFiltered
      ? (counts.unique_google_panos ?? counts.unique_panos)
      : counts.unique_panos,
    pano_age_stats: copyrightFiltered
      ? (latest.google_panos_age_stats ?? latest.all_panos_age_stats)
      : latest.all_panos_age_stats,
    capture_year_histogram: copyrightFiltered
      ? (histograms.google_panos ?? histograms.all_panos)
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
 * Could this capture date actually be true? The JS mirror of
 * analysis.plausible_capture_mask (issue #213).
 *
 * Contributor photospheres reach us with corrupt EXIF — production runs carry
 * panos dated 2611-09-01 and 1970-08-01 — and the published artifacts now drop
 * those before they are summarized. The raw run CSV that city.js streams does
 * NOT: it records what the provider said, deliberately, so the check has to
 * happen here as well or a 2611 pano is drawn at age −585.
 *
 * The upper bound is today rather than the snapshot's run date: nothing can be
 * captured after we look at it, and being lenient by the age of the snapshot
 * costs nothing when the values this rejects are centuries out.
 *
 * @param {?Date} date - Parsed capture date (from panoDateOrNull).
 * @param {?string} [provider="gsv"] - Provider key (see PROVIDERS).
 * @returns {boolean} True iff the date falls in the provider's possible range.
 */
function isPlausibleCaptureDate(date, provider) {
  if (!date || isNaN(date.getTime())) return false;
  // `provider || "gsv"` rather than a default parameter: a default only fires
  // on undefined, so an explicit null or "" — an unset provider threaded in
  // from a caller — skipped it, missed PROVIDERS, and fell through the ?? to
  // Mapillary's floor, accepting a GSV pano dated 2005. An UNKNOWN provider
  // name still lands on that loose floor deliberately — the same posture as
  // analysis._DEFAULT_EARLIEST_PLAUSIBLE_CAPTURE, though computed rather than
  // named after a provider (see LOOSEST_EARLIEST_PLAUSIBLE_CAPTURE).
  const earliest = PROVIDERS[provider || "gsv"]?.earliestPlausibleCapture
    ?? LOOSEST_EARLIEST_PLAUSIBLE_CAPTURE;
  const t = date.getTime();
  return t >= earliest.getTime() && t <= Date.now();
}

/**
 * Parse a pano capture date, returning null when the date is absent
 * (age_stats are all null for a 0-pano run). Guards against
 * `new Date(null)` silently rendering as the Unix epoch (12/31/1969)
 * instead of a "—"/"No data" placeholder (issue #122, #69 family).
 *
 * Date-ONLY strings are parsed as LOCAL midnight, not UTC:
 * `new Date("2023-01-01")` is UTC midnight, and reading it back with local
 * getters (toLocaleDateString, getFullYear) west of UTC shifts every date
 * back a day — and every January/year-precision capture date back a whole
 * YEAR (standardize_capture_date pins month/year precision to the 1st), so
 * US visitors saw those panos in the previous year's filter bucket and
 * color. Full timestamps (with a time component) still parse natively.
 *
 * All THREE ISO precisions are matched — "YYYY-MM-DD", "YYYY-MM", "YYYY" —
 * because city.js streams the run CSV itself and the legacy pre-2026 runs
 * carry MONTH precision, are never rewritten, and reach here verbatim (issue
 * #226). Matching only the full form sent those through `new Date("2022-09")`,
 * which is the very UTC parse the paragraph above exists to avoid, and at a
 * whole month's magnitude rather than a day's: west of UTC "2022-09" read back
 * as August and "2022-01" as 2021. Reduced precision is pinned to the 1st, the
 * same convention Python applies (standardize_capture_date, and
 * fileutils.load_city_csv_file's ISO8601 parse), so the map and the published
 * stats describe one population.
 *
 * The month and day groups are RANGE-bounded rather than `\d{2}`, and that is
 * the widening's own safety rail rather than fussiness. `new Date(y, m, d)`
 * ROLLS OVER: an unbounded month group turned "2022-13" into Jan 2023 and
 * "2022-00" into Dec 2021 — values `isPlausibleCaptureDate` then accepts, so a
 * corrupt field would have been drawn and bucketed as a real capture instead of
 * dropped, while Python's ISO8601 parse coerces both to NaT. A plausible wrong
 * date is worse than an honest null (the same rule KartaView's
 * `shot_date >= date_added` guard exists for). Bounded, they miss the regex and
 * fall through to `new Date`, which rejects them too — so JS and Python agree.
 * The day bound closes the same hole one field over, which predates the
 * widening: "2022-09-32" matched `\d{2}` and rolled to Oct 2.
 *
 * What the regex CANNOT close, stated so nobody reads it as closed: a
 * shape-valid but calendar-impossible date. "2022-02-30" is NaT in Python and
 * Mar 2 here, and the `new Date` fallback is no better (Mar 1, via the UTC
 * parse this function exists to avoid) — so there is no cheap answer, only a
 * per-row round-trip check for a value no writer in this repo can emit
 * (standardize_capture_date strptime-validates; both census decoders strftime).
 * The range bounds are free; that check would not be, and would not be
 * complete either.
 *
 * @param {?string} v - ISO date string, or null/undefined.
 * @returns {?Date} A Date (local midnight for date-only), or null when falsy.
 */
function panoDateOrNull(v) {
  if (!v) return null;
  const m = /^(\d{4})(?:-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?)?$/.exec(String(v));
  if (m) return new Date(Number(m[1]), Number(m[2] ?? 1) - 1, Number(m[3] ?? 1));
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
    CARTO_BASEMAP_KEY,
    addBasemapLayer,
    RENDER_CAP,
    PROVIDERS,
    LOOSEST_EARLIEST_PLAUSIBLE_CAPTURE,
    METRICS,
    isKnownProvider,
    isKnownMetric,
    parseFilterParam,
    getColor,
    coverageColor,
    recencyColor,
    FRESHNESS_BUCKETS,
    escapeHtml,
    isValidRunFilename,
    diffFilenameFor,
    isValidDiffFilename,
    getProviderFromFilename,
    fetchGzippedJson,
    fetchGzippedText,
    streetwalkManifestUrl,
    fetchStreetwalkManifest,
    DEFAULT_STREET_NETWORK_TYPE,
    STREET_NETWORK_LABELS,
    streetNetworkLabel,
    isKnownStreetNetworkType,
    lookupStreetwalk,
    mergeStreetwalkStats,
    adaptCityRecord,
    adaptCitiesPayload,
    isGoogleCopyright,
    isPlausibleCaptureDate,
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
