/**
 * streets.js — the Street-Level Coverage page (streets.html).
 *
 * Lists every published road-walk collection (issue #99) from the
 * `streetwalks.json.gz` sidecar manifest (issue #155), joined against the
 * `cities.json.gz` aggregate for display names and the run filename each row
 * links to.
 *
 * Why the join: the manifest is keyed by canonical `city_id` and carries no
 * human label and no grid-run filename, while `city.html` is addressed by run
 * filename (`city.html?file=…`). Only the aggregate has both, so a row can
 * only become a link if the city also has a published run. Rows whose city is
 * missing from the aggregate still render — they just don't link out.
 *
 * ONE ROW PER (CITY, NETWORK), providers as sub-columns (issue #250). Two
 * things could not share a row before: a city's GSV and Mapillary walks, which
 * measure the SAME sample points and are directly comparable — those are now
 * columns — and a city's `drive` and `all_public` walks, which divide by
 * DIFFERENT street-km denominators and must never sit in comparable columns.
 * So the network is a page-level selector (one network at a time) and the
 * providers pivot within it.
 *
 * Deliberately not a map: with a handful of walked cities, a second Leaflet
 * view would duplicate the overview's search/legend/scatter machinery for no
 * gain. Each row links to the city page, which already prefers the road-walk
 * artifact over the grid one via the same manifest.
 *
 * Depends on globals from streetscape-utils.js (loaded first): PROVIDERS,
 * STREETSCAPE_DATA_BASE_URL, fetchGzippedJson, fetchStreetwalkManifest,
 * adaptCitiesPayload, escapeHtml, DEFAULT_STREET_NETWORK_TYPE,
 * streetNetworkLabel — from table-utils.js: cityDisplayLabel, sortRowsBy,
 * formatCellNumber, coverageCellHtml, providerShortLabel, deltaCellHtml,
 * providerColumnGroup, rowHtmlFromColumns, createSortableTable — and from
 * table-controls.js: createTableControls.
 */

// ── Display helpers ───────────────────────────────────────────

/**
 * Build a "City, State, Country" label from an adapted city record.
 * Alias kept for this page's tests/callers; the canonical copy lives in
 * table-utils.js since the grid page needs it too.
 *
 * @param {Object} city - Adapted city record.
 * @returns {string}
 */
function cityLabel(city) {
  return cityDisplayLabel(city);
}

/**
 * Registry order — every provider the site KNOWS ABOUT.
 *
 * Not the same list as the one the table renders: a provider can be
 * registered and have walked nothing (KartaView is registered but has no road
 * walk at all — `build_streetwalk_rows` is Mapillary-specific in three
 * separate ways), and a leaf column or scope option for such a provider is a
 * column of em-dashes and a filter that selects no rows. See
 * `walkProvidersIn`.
 */
function walkProviders() {
  return Object.keys(PROVIDERS);
}

/**
 * The registered providers these walks actually CONTAIN, in registry order.
 *
 * This is what the columns, the presets and the "Collected by" options fan out
 * from — the manifest is the payload here, the way cities.json.gz is on the
 * grid page (issue #250 review).
 *
 * @param {Object[]} walks - Manifest walk records.
 * @returns {string[]}
 */
function walkProvidersIn(walks) {
  const present = new Set((walks ?? []).map((w) => w.provider));
  return walkProviders().filter((p) => present.has(p));
}

/**
 * The head-to-head pair the Δ columns compare, as [minuend, subtrahend]. Same
 * fixed-pair reasoning as grid.js's GRID_DELTA_PAIRS: "best − GSV" would have
 * a sign that means something different in every row.
 */
const STREET_DELTA_PAIRS = [["mapillary", "gsv"]];

/**
 * The first delta pair both of whose providers are present, or null.
 *
 * @param {string[]} [providers] - Providers present in the manifest.
 * @returns {?string[]}
 */
function streetDeltaPair(providers = walkProviders()) {
  return STREET_DELTA_PAIRS.find(([a, b]) => providers.includes(a) && providers.includes(b)) ?? null;
}

/**
 * Index the aggregate by `city_id` for every provider that appears in the
 * walks, so each row can be joined in one lookup.
 *
 * Adapting is per-provider (a v3 record holds an independent run series per
 * provider), so this adapts once per distinct provider rather than once per
 * row.
 *
 * @param {?Object} rawCities - The parsed cities.json.gz, or null.
 * @param {string[]} providers - Distinct provider keys to index.
 * @returns {Map<string, Object>} Keyed "provider|city_id".
 */
function indexCitiesByProvider(rawCities, providers) {
  const index = new Map();
  if (!rawCities) return index;
  for (const provider of providers) {
    const { cities } = adaptCitiesPayload(rawCities, provider);
    for (const city of cities) {
      if (!city.city_id) continue;
      index.set(`${provider}|${city.city_id}`, city);
      // Also index by city_id alone, for the display NAME only. A city can be
      // walked by a provider it has no grid run for (Mapillary street coverage
      // costs a handful of tiles, so it lands before a full census does), and
      // a city's name is provider-independent — falling back to it beats
      // showing a raw slug. The link is NOT taken from this entry: city.html
      // derives its provider from the run filename, so a cross-provider link
      // would open the wrong series.
      if (!index.has(city.city_id)) index.set(city.city_id, city);
    }
  }
  return index;
}

/** Format a nullable number for a table cell (table-utils alias). */
function num(value, digits = 0) {
  return formatCellNumber(value, digits);
}

/**
 * Cell for one provider's "since last walk" coverage delta: an em-dash for a
 * first walk (no change block in the manifest), else a signed
 * percentage-point figure whose title carries the comparison date and the edge
 * churn behind it.
 *
 * This is a WITHIN-provider comparison over time, and it is the reason this
 * group keeps one column per provider rather than gaining a cross-provider Δ
 * of its own: "GSV improved 4 points and Mapillary improved 1" is two facts
 * about two series, and their difference is not a third.
 *
 * An exact zero reads as "0.0 pp", unsigned — matching `deltaCellHtml` one
 * column over, where the reasoning is spelled out: "the walk found exactly
 * what the last one did" is a genuinely different fact from "it moved a
 * little", and "+0.0" claims a rise that did not happen. It carries the same
 * `delta-zero` hook, so the two cells are styled by one rule.
 *
 * @param {Object} row - From pivotStreetWalks.
 * @param {string} provider
 * @returns {{html: string, title?: string}} Cell parts (see providerCellHtml).
 */
function walkChangeCellHtml(row, provider) {
  const delta = row[`changeDelta_${provider}`];
  if (delta == null) return { html: "—" };
  const change = row[`change_${provider}`] ?? {};
  const sign = delta > 0 ? "+" : "";
  const tone = delta > 0 ? "delta-pos" : delta < 0 ? "delta-neg" : "delta-zero";
  const title =
    `Since ${change.from}: ${change.edges_gained_coverage ?? 0} streets gained ` +
    `coverage, ${change.edges_lost_coverage ?? 0} lost it`;
  return {
    html: `${sign}${delta.toFixed(1)} pp`,
    className: tone,
    title: escapeHtml(title),
  };
}

/**
 * Cell for the "City" column: the label, hyperlinked to the city page when
 * this row has a walk to link to.
 *
 * The target is the first registered provider that walked this (city,
 * network); each provider's own "Walked" sub-cell links to THAT provider's
 * walk, which is where a reader after a specific series goes.
 *
 * The link carries THIS row's network type: city.html selects the walk to draw
 * by network type and defaults to 'drive', so without it a "Roads + paths" row
 * would open the city's drive walk instead — or, for a city walked only
 * broadly, fall all the way back to the grid-attribution artifact, a different
 * metric entirely. Advertising a row the link cannot reach is worse than not
 * listing it.
 *
 * `title` carries the full, untruncated label either way — the cell itself is
 * ellipsis-truncated in CSS (data-table.css) because OSM/Nominatim labels are
 * unbounded and a long one alone can push the table past its measure.
 *
 * @param {Object} row - From pivotStreetWalks.
 * @returns {string} HTML for one <th scope="row">.
 */
function walkLabelCellHtml(row) {
  const label = escapeHtml(row.label);
  const content = row.filename ? cityPageLink(row.filename, row.networkType, label) : label;
  return `<th scope="row" title="${label}">${content}</th>`;
}

/**
 * An `<a>` into city.html for one run filename + network type.
 *
 * The `&network=` is load-bearing (see walkLabelCellHtml) and this is the one
 * place it is assembled, so the City cell and every per-provider "Walked" cell
 * cannot drift apart on it.
 *
 * @param {string} filename - A RUN csv.gz filename (not a walk artifact).
 * @param {string} networkType
 * @param {string} text - Already-escaped link text.
 * @param {?string} [title] - Already-escaped title attribute.
 * @returns {string}
 */
function cityPageLink(filename, networkType, text, title = null) {
  return (
    `<a class="streets-view-link"${title ? ` title="${title}"` : ""} ` +
    `href="city.html?file=${encodeURIComponent(filename)}` +
    `&network=${encodeURIComponent(networkType)}">${text}</a>`
  );
}

/**
 * The link factory every per-provider group on this page shares: open THAT
 * provider's walk of THIS row's network on the city page.
 *
 * Two things it has to get right. The filename comes only from the
 * `${provider}|${city_id}` index entry, never from the bare-city_id name
 * fallback — city.html derives its provider from the run filename, so a
 * cross-provider link opens the wrong series entirely. And the `&network=` is
 * load-bearing: city.html defaults to 'drive', so a "Roads + paths" row whose
 * link omits it opens a different walk, or falls all the way back to the
 * grid-attribution artifact.
 *
 * @param {string} provider
 * @returns {(row: Object) => ?{href: string, title: string}}
 */
function walkProviderLink(provider) {
  return (row) => {
    const file = row[`filename_${provider}`];
    if (!file) return null;
    const date = row[`runDate_${provider}`];
    return {
      href:
        `city.html?file=${encodeURIComponent(file)}` +
        `&network=${encodeURIComponent(row.networkType)}`,
      title: escapeHtml(
        `Open ${providerShortLabel(provider)}${date ? ` · ${date}` : ""} · ${row.networkLabel}`
      ),
    };
  };
}

// ── Columns ───────────────────────────────────────────────────

/**
 * The columns, in table order — generated from the PROVIDERS registry so a
 * third provider needs no edit here.
 *
 * `key` is the row-model field, `type` picks the comparator, `initial` is the
 * direction a first click applies (numbers read best-first, text reads A–Z),
 * and `cell(row)` renders that column's own cell — the header and the body are
 * both generated from this list so they cannot drift (see table-utils.js).
 *
 * City/state/country names come from OSM/Nominatim (publicly editable
 * third-party data) — escape everything data-derived entering innerHTML.
 *
 * @param {string[]} [providers] - Providers to give a leaf column, in order.
 *   The render path passes the ones the manifest contains; the default is the
 *   whole registry, which is what a caller with no manifest wants.
 * @returns {Object[]}
 */
function buildStreetColumns(providers = walkProviders()) {
  const pair = streetDeltaPair(providers);
  const [ahead, behind] = pair ?? [];
  const pairNames = pair
    ? `${providerShortLabel(ahead)} − ${providerShortLabel(behind)}`
    : "";

  return [
    {
      key: "label",
      label: "City",
      type: "text",
      initial: "asc",
      always: true,
      cell: walkLabelCellHtml,
    },
    ...providerColumnGroup({
      providers,
      id: "cov",
      groupLabel: "360° street-km (%)",
      // States the rule rather than counting who is in the group: this string
      // is also every leaf's default tooltip, so "Both providers" was already
      // one collected provider away from being false (#296 review).
      groupTitle:
        "Share of street-km covered by 360° imagery. Every provider walks the SAME sample " +
        "points on the same frozen network, so these are directly comparable.",
      keyFor: (p) => `pct_${p}`,
      cellFor: (p) => (row) => coverageCellParts(row[`pct_${p}`], { compact: true }),
      linkFor: walkProviderLink,
      initial: "desc",
      unit: "%",
      digits: 1,
      delta: pair && {
        key: "deltaPct",
        unit: " pp",
        title: `${pairNames}, in percentage points. Positive means Mapillary covers more street-km.`,
      },
    }),
    ...providerColumnGroup({
      providers,
      id: "covAny",
      groupLabel: "Any imagery (%)",
      // Same misattribution grid.js carried until #295: one shared string
      // naming ONE provider was hung verbatim on every leaf. Left unfixed
      // here it would have re-landed the moment the KartaView road-walk
      // collector (#258) published its first walk, on the provider whose flat
      // imagery is the larger half of its data (#296 review).
      groupTitle:
        "Share of street-km covered by imagery of ANY kind — 360° panoramas plus flat/" +
        "perspective images, for a provider that publishes both",
      keyFor: (p) => `pctAny_${p}`,
      leafTitle: (p) => anyImageryLeafTitle(p, "Equals the 360° street-km number"),
      cellFor: (p) => (row) => coverageCellParts(row[`pctAny_${p}`], { compact: true }),
      linkFor: walkProviderLink,
      initial: "desc",
      unit: "%",
      digits: 1,
      delta: pair && {
        key: "deltaPctAny",
        unit: " pp",
        title: `${pairNames}, in percentage points. Positive means Mapillary covers more street-km with imagery of any kind.`,
      },
    }),
    ...providerColumnGroup({
      providers,
      id: "walked",
      groupLabel: "Walked",
      groupTitle: "Date of each provider's latest walk of this network",
      keyFor: (p) => `runDate_${p}`,
      cellFor: (p) => (row) => ({
        html: row[`runDate_${p}`] == null ? "—" : escapeHtml(row[`runDate_${p}`]),
      }),
      linkFor: walkProviderLink,
      type: "text",
      initial: "desc",
    }),
    ...providerColumnGroup({
      providers,
      id: "age",
      groupLabel: "Median age (yrs)",
      groupTitle:
        "Median age of the imagery covering this walk's streets. Stored rather than " +
        "derived — a median of the per-class medians is not the median.",
      keyFor: (p) => `medianAge_${p}`,
      cellFor: (p) => (row) => ({
        html: row[`medianAge_${p}`] == null ? "—" : `${num(row[`medianAge_${p}`], 1)} yrs`,
      }),
      linkFor: walkProviderLink,
      initial: "asc",
      unit: " yrs",
      digits: 1,
    }),
    // Walk-to-walk change (issue #101): each provider against ITS OWN previous
    // walk. Deliberately no Δ leaf — a difference between two providers' own
    // improvements is not a quantity anyone asked for.
    ...providerColumnGroup({
      providers,
      id: "change",
      groupLabel: "Δ since last walk (pp)",
      groupTitle:
        "Change in that provider's 360° street-km coverage since ITS previous walk, in " +
        "percentage points. Blank for first walks. Never a cross-provider comparison.",
      keyFor: (p) => `changeDelta_${p}`,
      cellFor: (p) => (row) => walkChangeCellHtml(row, p),
      linkFor: walkProviderLink,
      initial: "desc",
      unit: " pp",
      digits: 1,
    }),
    ...providerColumnGroup({
      providers,
      id: "coveredKm",
      groupLabel: "Covered km",
      groupTitle: "Kilometres of street covered by 360° imagery",
      keyFor: (p) => `lengthKmCovered_${p}`,
      cellFor: (p) => (row) => ({
        html:
          row[`lengthKmCovered_${p}`] == null ? "—" : `${num(row[`lengthKmCovered_${p}`], 1)} km`,
      }),
      linkFor: walkProviderLink,
      initial: "desc",
      unit: " km",
      digits: 1,
    }),
    ...providerColumnGroup({
      providers,
      id: "coveredKmAny",
      groupLabel: "Covered km (any)",
      groupTitle: "Kilometres of street covered by any imagery, including flat/perspective",
      keyFor: (p) => `lengthKmCoveredAny_${p}`,
      // The same any-imagery branch as covAny above, one column further right
      // and in kilometres rather than percent: a 360°-only provider's two
      // covered-km columns are identical numbers, and the tooltip is the only
      // thing saying why.
      leafTitle: (p) => anyImageryLeafTitle(p, "Equals the 360° covered km"),
      cellFor: (p) => (row) => ({
        html:
          row[`lengthKmCoveredAny_${p}`] == null
            ? "—"
            : `${num(row[`lengthKmCoveredAny_${p}`], 1)} km`,
      }),
      linkFor: walkProviderLink,
      initial: "desc",
      unit: " km",
      digits: 1,
    }),
    ...providerColumnGroup({
      providers,
      id: "fully",
      groupLabel: "Fully covered",
      groupTitle: "Streets covered end to end, per provider",
      keyFor: (p) => `fullyCovered_${p}`,
      cellFor: (p) => (row) => ({ html: num(row[`fullyCovered_${p}`]) }),
      linkFor: walkProviderLink,
      initial: "desc",
    }),
    ...providerColumnGroup({
      providers,
      id: "spacing",
      groupLabel: "Sample spacing (m)",
      groupTitle: "Along-edge sample spacing each provider's walk used",
      keyFor: (p) => `spacing_${p}`,
      cellFor: (p) => (row) => ({
        html: row[`spacing_${p}`] == null ? "—" : `${num(row[`spacing_${p}`])} m`,
      }),
      linkFor: walkProviderLink,
      initial: "asc",
      unit: " m",
    }),
    // Properties of the OSM NETWORK rather than of a walk, so one column
    // apiece rather than one per provider. See pivotStreetWalks for the caveat
    // the title carries.
    {
      key: "lengthKm",
      label: "Street km",
      type: "number",
      initial: "desc",
      unit: " km",
      digits: 1,
      title:
        "Total length of the walked network, in kilometres. A property of the OSM network, " +
        "not of a provider — but each walk re-derives it from the frozen graph, so two " +
        "providers' figures can differ slightly; the first available is shown.",
      cell: (r) => `<td>${r.lengthKm == null ? "—" : `${num(r.lengthKm, 1)} km`}</td>`,
    },
    {
      key: "edges",
      label: "Streets",
      type: "number",
      initial: "desc",
      title:
        "Street segments in the walked network. Like Street km, a network property that each " +
        "walk re-derives; the first available is shown.",
      cell: (r) => `<td>${num(r.edges)}</td>`,
    },
  ];
}

/**
 * The full-registry build: what the page would render if every registered
 * provider had walked something. The static vocabulary — `?sort=` keys,
 * `?cols=` names — is taken from here, and it is the default for callers with
 * no manifest. What actually renders is built per manifest in
 * `renderStreetWalks`, from the providers the manifest contains.
 */
const STREET_COLUMNS = buildStreetColumns();

/**
 * Every leaf key of one grouped metric, in table order.
 *
 * @param {string} id - Group id.
 * @param {Object[]} [columns] - The build to read; defaults to full-registry.
 */
function streetGroupKeys(id, columns = STREET_COLUMNS) {
  return columns.filter((c) => c.group?.id === id).map((c) => c.key);
}

/**
 * Column presets. The first is the default and must fit the page's content
 * measure (1500px page − the 280px sidebar) without horizontal scrolling.
 *
 * Built from a column list rather than fixed, so a preset naming a grouped
 * metric lists exactly the leaves that were built.
 *
 * @param {Object[]} [columns] - The build to draw leaf keys from.
 * @returns {Object[]}
 */
function buildStreetPresets(columns = STREET_COLUMNS) {
  const groupKeys = (id) => streetGroupKeys(id, columns);
  return [
    {
      id: "overview",
      label: "Overview",
      // pctAny stays out of the default view now that it is a whole GROUP rather
      // than one column; the Δ in the 360° group is the headline comparison and
      // "Kilometres" is one click away.
      title: "The headline read: who walked what, how much of it, and how fresh",
      columns: [
        ...groupKeys("cov"),
        ...groupKeys("walked"),
        ...groupKeys("age"),
        "lengthKm",
      ],
    },
    {
      id: "kilometres",
      label: "Kilometres",
      title: "Absolute street length rather than shares (schema v12)",
      columns: [
        ...groupKeys("cov"),
        "lengthKm",
        ...groupKeys("coveredKm"),
        ...groupKeys("coveredKmAny"),
      ],
    },
    {
      id: "change",
      label: "Change",
      title: "Walk-to-walk movement (issue #101); blank until a city's second walk lands",
      columns: [
        ...groupKeys("cov"),
        ...groupKeys("walked"),
        ...groupKeys("change"),
      ],
    },
    {
      id: "network",
      label: "Network",
      title: "The shape of the walked network itself, and how each provider sampled it",
      columns: [
        ...groupKeys("spacing"),
        "edges",
        ...groupKeys("fully"),
        "lengthKm",
      ],
    },
  ];
}

/**
 * Filters offered in the sidebar.
 *
 * @param {string[]} [providers] - Providers to offer as scopes, in order.
 *   The render path passes the ones the manifest contains.
 * @returns {Object[]}
 */
function buildStreetFilters(providers = walkProviders()) {
  return [
    {
      // FIRST in the sidebar, and a page-level selector rather than an ordinary
      // narrowing: two network types are two different street-km denominators,
      // so there is no "all networks" reading that would not stack incomparable
      // numbers in one column. `defaultValue` is what makes absence of the
      // param mean 'drive' rather than "no filter"; old ?network=all_public
      // links keep working.
      key: "network",
      label: "Network",
      type: "select",
      defaultValue: "drive",
      options: [
        { value: "drive", label: "Roads" },
        { value: "all_public", label: "Roads + paths" },
      ],
      test: (row, value) => row.networkType === value,
    },
    {
      key: "provider",
      label: "Collected by",
      type: "select",
      anyLabel: "Any provider",
      // Collected rather than registered — see GRID_FILTERS for why an option
      // matching zero rows is worse here than elsewhere: this select is also
      // the SCOPE the numeric sliders read through.
      options: providers
        .map((value) => ({ value, label: PROVIDERS[value].label }))
        .concat([{ value: SCOPE_MULTI, label: "2+ providers" }]),
      test: (row, value) =>
        value === SCOPE_MULTI ? row.providers.length > 1 : row.providers.includes(value),
    },
    // Follows the scope above — see GRID_FILTERS for why.
    {
      key: "cov",
      label: "360° street-km %",
      type: "histogram-range",
      field: "pctBest",
      min: 0,
      max: 100,
      unit: "%",
      digits: 1,
      ...scopedNumericFilter({
        base: "pct",
        bestField: "pctBest",
        label: "360° street-km %",
        anyLabel: "any provider reaches",
      }),
    },
    // NOT scoped: street length is a property of the OSM network, not of a
    // provider's walk of it, so there is no per-provider column to read.
    {
      key: "km",
      label: "Street km",
      type: "histogram-range",
      field: "lengthKm",
      min: 0,
      unit: " km",
      digits: 1,
    },
    {
      key: "changed",
      label: "Has Δ since last walk",
      type: "boolean",
      title:
        "Cities walked at least twice, so a change could be computed (issue #101). Follows " +
        "the Collected by scope: with a provider selected it asks about THAT provider's " +
        "second walk, not anyone's.",
      test: (row) => walkProviders().some((p) => row[`changeDelta_${p}`] != null),
      // Same scope contract as the numeric filters: "walked twice" is as
      // incomplete a question as "coverage over 80%" until you say by whom.
      testFor: (values) => {
        const provider = scopedProvider(values);
        return provider
          ? (row) => row[`changeDelta_${provider}`] != null
          : (row) => walkProviders().some((p) => row[`changeDelta_${p}`] != null);
      },
      labelFor: (values) => {
        const provider = scopedProvider(values);
        return provider
          ? `Has Δ since last walk — ${providerShortLabel(provider)}`
          : "Has Δ since last walk — any provider";
      },
    },
  ];
}

/** The full-registry builds, for the static vocabulary and for the tests. */
const STREET_PRESETS = buildStreetPresets();
const STREET_FILTERS = buildStreetFilters();

/** Row fields the free-text search box looks at. */
const STREET_SEARCH_FIELDS = ["label", "cityId", "providersLabel", "networkLabel"];

/**
 * Default sort: best GSV 360° coverage first.
 *
 * Deliberately the GSV leaf rather than `pctBest`: the page has always opened
 * on coverage-descending, and `pctBest` is a filter field with no column of
 * its own — sorting by a column the reader cannot see is exactly what
 * createSortableTable's fallback exists to prevent. Privileging one provider
 * in the default order is a real (small) asymmetry, taken knowingly because
 * GSV is the series every city has.
 */
const DEFAULT_SORT = { key: "pct_gsv", dir: "desc" };

// ── Row model ─────────────────────────────────────────────────

/** a − b, or null unless BOTH operands are present (see grid.js's deltaOf). */
function walkDeltaOf(a, b) {
  return a == null || b == null ? null : a - b;
}

/**
 * Pivot the manifest's walks into one row per (city, NETWORK), with
 * per-provider sub-fields.
 *
 * The network stays a row key rather than becoming more columns because its
 * two values divide by different street-km denominators: putting a 'drive'
 * percentage beside an 'all_public' one under a shared header would invite
 * exactly the comparison that is not valid. Providers, by contrast, walk the
 * SAME deterministic sample points on the same frozen network, so their
 * numbers belong side by side.
 *
 * @param {Object[]} walks - Manifest walk records.
 * @param {Map<string, Object>} index - From indexCitiesByProvider.
 * @returns {Object[]} Row models.
 */
function pivotStreetWalks(walks, index) {
  const byKey = new Map();
  // Narrowed to what this manifest CONTAINS, so a registered-but-unwalked
  // provider gets no row keys — and therefore no leaf columns, no preset
  // entries and no scope option. See walkProvidersIn.
  const providers = walkProvidersIn(walks);

  for (const walk of walks) {
    const cityId = walk.city_id;
    const networkType = walk.network_type ?? DEFAULT_STREET_NETWORK_TYPE;
    const rowKey = `${cityId}|${networkType}`;
    const provider = walk.provider;

    let row = byKey.get(rowKey);
    if (!row) {
      row = {
        rowKey,
        cityId,
        // The display name may come from ANY provider's aggregate record (a
        // city's name is provider-independent) — unlike the link, which may
        // not. See indexCitiesByProvider.
        label: cityId,
        networkType,
        networkLabel: streetNetworkLabel(walk.network_type),
        providers: [],
        providersLabel: "",
        providerCount: 0,
        // Always present, so "this manifest has no Δ pair" reads as null
        // rather than as a missing field.
        deltaPct: null,
        deltaPctAny: null,
        lengthKm: null,
        edges: null,
        filename: null,
      };
      for (const p of providers) {
        row[`pct_${p}`] = null;
        row[`pctAny_${p}`] = null;
        row[`runDate_${p}`] = null;
        row[`spacing_${p}`] = null;
        row[`medianAge_${p}`] = null;
        row[`lengthKmCovered_${p}`] = null;
        row[`lengthKmCoveredAny_${p}`] = null;
        row[`change_${p}`] = null;
        row[`changeDelta_${p}`] = null;
        row[`fullyCovered_${p}`] = null;
        row[`filename_${p}`] = null;
      }
      byKey.set(rowKey, row);
    }

    row.providers.push(provider);
    row[`pct_${provider}`] = walk.coverage_pct_by_length ?? null;
    // Any-imagery street coverage: Mapillary only (flat/perspective imagery
    // counts as covered too). For GSV it equals the 360° number, and it is
    // null for walks collected before the field existed.
    row[`pctAny_${provider}`] = walk.coverage_pct_by_length_any ?? null;
    row[`runDate_${provider}`] = walk.run_date ?? null;
    row[`spacing_${provider}`] = walk.spacing_m ?? null;
    // Absolute lengths and median covered age (schema v12). NULL on walks
    // cataloged before v12 and not yet backfilled.
    row[`medianAge_${provider}`] = walk.median_covered_age_years ?? null;
    row[`lengthKmCovered_${provider}`] = walk.length_km_covered ?? null;
    row[`lengthKmCoveredAny_${provider}`] = walk.length_km_covered_any ?? null;
    // "Since last walk" change block (issue #101). Absent from the manifest
    // for first walks, so both stay null and the cell renders an em-dash.
    row[`change_${provider}`] = walk.change ?? null;
    row[`changeDelta_${provider}`] = walk.change?.coverage_pct_by_length_delta ?? null;
    row[`fullyCovered_${provider}`] = walk.edges_fully_covered ?? null;
    // ONLY the provider-keyed entry, never the bare-city_id name fallback:
    // city.html derives its provider from the run filename, so a
    // cross-provider link opens the wrong series entirely.
    row[`filename_${provider}`] = index.get(`${provider}|${cityId}`)?.data_file?.filename ?? null;

    // Network properties: first walk to report each one wins. Each walk
    // re-derives them from the same frozen graph, so two providers can differ
    // slightly — the column's title says so rather than pretending otherwise.
    row.lengthKm ??= walk.length_km ?? null;
    row.edges ??= walk.edges ?? null;

    const named = index.get(`${provider}|${cityId}`) ?? index.get(cityId);
    if (named && row.label === cityId) row.label = cityLabel(named);
  }

  const pair = streetDeltaPair(providers);
  const rows = [...byKey.values()];
  for (const row of rows) {
    row.providerCount = row.providers.length;
    row.providersLabel = row.providers.map(providerShortLabel).join(", ");
    if (pair) {
      const [a, b] = pair;
      row.deltaPct = walkDeltaOf(row[`pct_${a}`], row[`pct_${b}`]);
      row.deltaPctAny = walkDeltaOf(row[`pctAny_${a}`], row[`pctAny_${b}`]);
    }
    const values = providers
      .map((p) => row[`pct_${p}`])
      .filter((v) => typeof v === "number" && Number.isFinite(v));
    row.pctBest = values.length ? Math.max(...values) : null;
    // The City cell opens the first provider that walked this (city, network)
    // AND has a published run to address.
    row.filename = providers.map((p) => row[`filename_${p}`]).find((f) => f) ?? null;
  }
  return rows;
}

/**
 * Sort row models by one column (table-utils.sortRowsBy over this page's
 * columns). Alias kept for this page's tests/callers.
 *
 * The tie key is `rowKey`, not `cityId`: a city appears once per network type,
 * so city_id alone no longer identifies a row and ties would break
 * arbitrarily between a city's two networks.
 *
 * @param {Object[]} rows - Row models from pivotStreetWalks.
 * @param {string} key - A STREET_COLUMNS key.
 * @param {"asc"|"desc"} dir
 * @returns {Object[]} A new sorted array.
 */
function sortRows(rows, key, dir = "desc") {
  return sortRowsBy(STREET_COLUMNS, rows, key, dir, "rowKey");
}

// ── Rendering ─────────────────────────────────────────────────

/**
 * Build one table row from a row model.
 *
 * @param {Object} row - From pivotStreetWalks.
 * @param {Object[]} [columns] - Visible columns; defaults to all of them.
 * @returns {string} HTML for one <tr>.
 */
function walkRowHtml(row, columns = STREET_COLUMNS) {
  return rowHtmlFromColumns(columns, row);
}

// The table + controls controllers (created on first render so a header click
// or a filter change can repaint without refetching or re-joining).
let streetsTable = null;
let streetsControls = null;

/**
 * Render the table (or the empty state) from a manifest + aggregate.
 *
 * @param {?Object} manifest - Parsed streetwalks.json.gz, or null.
 * @param {?Object} rawCities - Parsed cities.json.gz, or null.
 */
function renderStreetWalks(manifest, rawCities) {
  const statusEl = document.getElementById("streets-status");
  const wrapEl = document.getElementById("streets-table-wrap");
  const walks = Array.isArray(manifest?.walks) ? manifest.walks : [];

  if (walks.length === 0) {
    // A real code path, not a defensive branch: a deployment that has never
    // run the collector publishes no manifest at all.
    statusEl.textContent =
      "No road-walk collections have been published yet. Street-level coverage is " +
      "collected on the same schedule as the grid, city by city; check back soon.";
    return;
  }

  const providers = walkProvidersIn(walks);
  const index = indexCitiesByProvider(rawCities, providers);
  const rows = pivotStreetWalks(walks, index);

  // Built from the providers THIS manifest carries, not from the registry: a
  // registered-but-unwalked provider would otherwise contribute a leaf to
  // every metric group (nine more columns for KartaView alone), all of them
  // em-dashes, plus a scope option that matches no rows.
  const columns = buildStreetColumns(providers);
  streetsTable ??= createSortableTable({
    columns,
    defaultSort: DEFAULT_SORT,
    theadEl: document.getElementById("streets-thead"),
    tbodyEl: document.getElementById("streets-tbody"),
    // city_id no longer identifies a row: a city appears once per network.
    tieKey: "rowKey",
  });
  streetsControls ??= createTableControls({
    rootEl: document.getElementById("streets-controls"),
    table: streetsTable,
    columns,
    presets: buildStreetPresets(columns),
    filters: buildStreetFilters(providers),
    searchFields: STREET_SEARCH_FIELDS,
    layout: "sidebar",
    showDistributionStrip: false,
    onChange: (shown, all, state) => updateStreetsCaption(shown, all, manifest, state),
  });
  streetsControls.setRows(rows);

  statusEl.hidden = true;
  wrapEl.hidden = false;
}

/**
 * Keep the caption reporting what is actually on screen.
 *
 * It names the ACTIVE NETWORK, and counts against the rows of that network
 * only: `allRows` holds both networks, so "3 of 7" would compare the visible
 * roads rows against a total that includes roads-and-paths rows the selector
 * has deliberately excluded.
 *
 * @param {Object[]} shown - Filtered rows.
 * @param {Object[]} all - Every row, both networks.
 * @param {?Object} manifest - For the generated-at stamp.
 * @param {{values: Object}} [state] - Control state snapshot.
 */
function updateStreetsCaption(shown, all, manifest, state) {
  const networkType = state?.values?.network ?? DEFAULT_STREET_NETWORK_TYPE;
  const inNetwork = all.filter((row) => row.networkType === networkType).length;
  const label = streetNetworkLabel(networkType);
  const noun = `${inNetwork === 1 ? "city" : "cities"} walked on ${label}`;
  // Through formatCellNumber, matching updateGridCaption: the two captions are
  // twins on twin pages, and this one is the half that printed a raw 1187.
  const counts =
    shown.length === inNetwork
      ? `${num(inNetwork)} ${noun}`
      : `${num(shown.length)} of ${num(inNetwork)} ${noun}`;
  document.getElementById("streets-caption").textContent =
    counts +
    (manifest?.generated_at
      ? ` · manifest updated ${new Date(manifest.generated_at).toLocaleString()}`
      : "");
}

// ── Data loading ──────────────────────────────────────────────

/** Fetch the manifest + aggregate, then render. */
async function loadStreetWalks() {
  try {
    // The aggregate is only needed for labels and links, so a failure there
    // still yields a useful (if unlinked) table — but a manifest failure
    // means there is nothing to list at all.
    const [manifest, rawCities] = await Promise.all([
      fetchStreetwalkManifest(),
      fetchGzippedJson(STREETSCAPE_DATA_BASE_URL + "cities.json.gz").catch((e) => {
        console.warn("Could not load cities.json.gz; rows will not link out:", e.message);
        return null;
      }),
    ]);
    renderStreetWalks(manifest, rawCities);
  } catch (error) {
    console.error("Error loading street-level coverage:", error);
    document.getElementById("streets-status").textContent =
      "Error loading street-level coverage. Please check the console for details.";
  }
}

// Guarded so `require`ing this file in the Node unit tests (which have no
// document) exercises the pure helpers without trying to load anything.
if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", loadStreetWalks);
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    cityLabel,
    indexCitiesByProvider,
    pivotStreetWalks,
    walkProviders,
    walkProvidersIn,
    buildStreetColumns,
    buildStreetPresets,
    buildStreetFilters,
    sortRows,
    num,
    walkChangeCellHtml,
    walkProviderLink,
    walkRowHtml,
    streetDeltaPair,
    renderStreetWalks,
    updateStreetsCaption,
    STREET_COLUMNS,
    STREET_PRESETS,
    STREET_FILTERS,
    STREET_SEARCH_FIELDS,
    STREET_DELTA_PAIRS,
    DEFAULT_SORT,
  };
}
