/**
 * grid.js — the Grid Coverage page (grid.html).
 *
 * Lists every city's grid-run coverage from the cities.json.gz aggregate as a
 * sortable table — the tabular counterpart of the overview map, the way
 * streets.html is the tabular view of the road walks.
 *
 * ONE ROW PER CITY, providers as sub-columns (issue #250). It used to be one
 * row per (city, provider), which defeated the page's own headline question:
 * sorting by any metric scattered a city's two series to opposite ends of the
 * table, so "does Mapillary beat GSV here?" could not be read off the screen
 * at all. The old "Multiple providers" checkbox existed only to FIND
 * comparable cities, because the layout could not SHOW the comparison. Pivoted,
 * the two numbers sit side by side under one grouped header and a signed Δ
 * column answers the question directly.
 *
 * Every per-provider column is generated from the PROVIDERS registry, so a
 * third provider is a registry edit rather than a column-list edit. The Δ
 * columns are the deliberate exception — see GRID_DELTA_PAIRS.
 *
 * The growth-screen group (issue #349) is the other exception: it fans out over
 * the providers the SCREEN document carries, which is a different list from
 * the collected one — see `gridScreenColumns`.
 *
 * Depends on globals from streetscape-utils.js (loaded first): PROVIDERS,
 * STREETSCAPE_DATA_BASE_URL, fetchGzippedJson, adaptCitiesPayload,
 * escapeHtml, fetchProviderScreen, screenedProviders, lookupScreen,
 * screenVerdict — from table-utils.js: cityDisplayLabel, sortRowsBy,
 * formatCellNumber, coverageCellHtml, providerShortLabel, deltaCellHtml,
 * providerColumnGroup, rowHtmlFromColumns, createSortableTable — and from
 * table-controls.js: createTableControls.
 */

/**
 * Registry order — every provider the site KNOWS ABOUT.
 *
 * Not the same list as the one the table renders. A provider can be registered
 * and publish nothing (KartaView is registered and, since #248, a scheduler
 * channel whose membership is opt-in, so essentially no city carries a
 * KartaView run until an operator enrolls one), and a
 * leaf column, a preset entry or a scope option for such a provider is a
 * column of em-dashes and a filter that selects no rows. `pivotGridRows`
 * reports which providers the payload actually CONTAINS and the render path
 * builds from that; this is the fallback for callers with no payload in hand.
 */
function gridProviders() {
  return Object.keys(PROVIDERS);
}

/**
 * The head-to-head pair the Δ columns compare, as [minuend, subtrahend].
 *
 * A FIXED pair, not "best − GSV": best's identity changes from row to row, so
 * the sign of such a column would mean something different in every one. With
 * a fixed pair the sign is interpretable — positive means Mapillary is ahead,
 * everywhere in the table.
 *
 * ONE pair, deliberately. The row keys are the bare `deltaPct` /
 * `deltaPctAny` / `deltaMedianAge` that `?sort=` and the `dcov` filter name,
 * so a second pair means widening those keys and the URL vocabulary, not
 * appending here. A third provider still gets its own sub-columns in every
 * group automatically; it just gets no Δ until someone makes that choice.
 * Filtered by COLLECTION, so a payload missing either provider simply has no Δ
 * columns rather than columns of em-dashes.
 */
const GRID_DELTA_PAIRS = [["mapillary", "gsv"]];

/**
 * The first delta pair both of whose providers are present, or null.
 *
 * @param {string[]} [providers] - Providers present in the payload.
 * @returns {?string[]}
 */
function gridDeltaPair(providers = gridProviders()) {
  return GRID_DELTA_PAIRS.find(([a, b]) => providers.includes(a) && providers.includes(b)) ?? null;
}

// ── Cells ─────────────────────────────────────────────────────

/**
 * The link factory every per-provider group on this page shares: open THAT
 * provider's latest run on the city page.
 *
 * city.html derives its provider from the run filename, so these cells are the
 * only place a reader can ask for one specific series — the City cell can open
 * exactly one of them. Null when the provider has no published run here, so a
 * cell of em-dashes is never a link to nowhere.
 *
 * @param {string} provider
 * @returns {(row: Object) => ?{href: string, title: string}}
 */
function gridProviderLink(provider) {
  return (row) => {
    const file = row[`filename_${provider}`];
    if (!file) return null;
    const date = row[`collected_${provider}`];
    return {
      href: `city.html?file=${encodeURIComponent(file)}`,
      title: escapeHtml(
        `Open ${providerShortLabel(provider)}${date ? ` · ${date}` : ""} for this city`
      ),
    };
  };
}

/**
 * Cell for the "City" column: the label, hyperlinked to the city page when
 * this row has a published run to link to.
 *
 * The link target is the first registered provider that has a run — the row is
 * a city now, so it has no single provider of its own. Each provider's own
 * "Last collected" sub-cell carries the link to THAT provider's run, which is
 * where a reader who wants a specific series goes.
 *
 * A row with nothing to link to still renders, just as plain text — the same
 * degrade-not-disappear posture the old placeholder cell had.
 *
 * `title` carries the full, untruncated label either way — the cell itself is
 * ellipsis-truncated in CSS (data-table.css) because OSM/Nominatim labels are
 * unbounded and a long one alone can push the table past its measure.
 *
 * @param {Object} row - From pivotGridRows.
 * @returns {string} HTML for one <th scope="row">.
 */
function gridLabelCellHtml(row) {
  const label = escapeHtml(row.label);
  // The visible text is the abbreviated label ("Dublin, IN, US"); the tooltip
  // is the spelled-out one, so the state and country a reader may not know by
  // code are always one hover away — the same contract the ellipsis already
  // had, now covering the abbreviation as well as the truncation.
  const title = escapeHtml(row.fullLabel ?? row.label);
  const content = row.filename
    ? `<a class="streets-view-link" href="city.html?file=${encodeURIComponent(row.filename)}">${label}</a>`
    : label;
  return `<th scope="row" title="${title}">${content}</th>`;
}

// ── The growth screen (issue #349) ────────────────────────────

/**
 * The tooltip a positive ("hint") or flat-only screen cell carries.
 *
 * The artifact's own `caveat` goes in VERBATIM rather than a paraphrase kept
 * here: it is the writer's statement of what the number may be read as, and a
 * copy in this file would drift the first time the writer's wording moved
 * (it already has once, #406). A document with no caveat still gets the one
 * sentence the asymmetry cannot do without.
 *
 * @param {Object} row - From pivotGridRows.
 * @param {string} provider
 * @param {?Object} entry - `doc.providers[provider]`.
 * @returns {string} Plain text (escape before it enters an attribute).
 */
function gridScreenHintTitle(row, provider, entry) {
  const date = row[`screenDate_${provider}`];
  const lead =
    row[`screenVerdict_${provider}`] === "flat"
      ? `Screened ${date}: no 360° imagery in or around this city, but at most ` +
        `${formatCellNumber(row[`screenAny_${provider}`])} flat (non-360°) pictures, ` +
        "so it is worth measuring for any-imagery coverage, not for 360°."
      : `Screened ${date}: at most ${formatCellNumber(row[`screen_${provider}`])} 360° pictures ` +
        "in or around this city.";
  const parts = [
    lead,
    entry?.caveat ?? "Upper bounds, not counts: only a collection run measures coverage.",
  ];
  const first = row[`screenFirstPositive_${provider}`];
  if (first) {
    // The writer derives this date from the ANY-imagery bound
    // (`db.get_provider_screen_firsts`, `pictures_upper_bound > 0`), so under
    // a 360° bound it must say so: flat imagery may have arrived first.
    // And the writer's own caveat on it: a city already positive at the FIRST
    // screen records when we started looking, not when imagery arrived.
    parts.push(
      first === entry?.first_screen_date
        ? `Positive (any imagery) since the first screen (${first}), which is when watching ` +
            "began, not when the imagery arrived."
        : `First screened positive (any imagery) ${first}.`
    );
  }
  return parts.join(" ");
}

/**
 * The warning that applies to THIS cell, or null.
 *
 * `instrument.cell_warning` describes the LATEST screen only, and is present
 * only when that pass read hexagons at an unexpected resolution — including
 * the override case where a zero is NOT conclusive. A record from an older
 * screen date was not read by that pass, so the warning is not hung on it.
 *
 * @param {Object} row - From pivotGridRows.
 * @param {string} provider
 * @param {?Object} entry - `doc.providers[provider]`.
 * @returns {?string}
 */
function gridScreenWarning(row, provider, entry) {
  const warning = entry?.instrument?.cell_warning;
  return warning && row[`screenDate_${provider}`] === entry?.latest_screen_date ? warning : null;
}

/**
 * Cell parts for one provider's screen leaf.
 *
 * The asymmetry is the whole rendering: a zero is a FACT and says so in words,
 * a positive number is a HINT and is printed as a bound ("≤ N"), never as a
 * bare count a reader could take for coverage. There are two zeros, kept
 * apart: "0 · none" (no imagery of any kind, the conclusive zero) and
 * "0 · flat only" (no 360°, but flat pictures an any-imagery reading counts).
 *
 * @param {Object} row - From pivotGridRows.
 * @param {string} provider
 * @param {?Object} entry - `doc.providers[provider]`.
 * @returns {{html: string, className: string, title?: string}}
 */
function gridScreenCellParts(row, provider, entry) {
  const verdict = row[`screenVerdict_${provider}`];
  if (verdict == null) return { html: "—", className: "screen-cell" };
  const warning = gridScreenWarning(row, provider, entry);
  if (verdict === "none") {
    const title =
      `Screened ${row[`screenDate_${provider}`]}: no imagery of any kind in or around this city` +
      (warning ? `. ${warning}` : " (a zero is conclusive)");
    return { html: "0 · none", className: "screen-cell screen-none", title: escapeHtml(title) };
  }
  const hint = gridScreenHintTitle(row, provider, entry);
  const title = escapeHtml(warning ? `${hint} ${warning}` : hint);
  if (verdict === "flat") {
    return { html: "0 · flat only", className: "screen-cell screen-flat", title };
  }
  return {
    html: `≤ ${formatCellNumber(row[`screen_${provider}`])}`,
    className: "screen-cell screen-hint",
    title,
  };
}

/**
 * The growth-screen column group, one leaf per SCREENED provider.
 *
 * Built from the screen document's providers, never the collected list or
 * the registry: a provider can be screened and never collected here, and vice
 * versa. No Δ — two providers' upper bounds over different instruments answer
 * nothing by subtraction. Sorting is allowed; the header says what the top of
 * a descending sort means, because that is the misreading this group invites.
 *
 * @param {?Object} screenDoc - Parsed provider_screen.json.gz, or null.
 * @returns {Object[]} Column descriptors (empty with no screen).
 */
function gridScreenColumns(screenDoc) {
  const screened = screenedProviders(screenDoc);
  if (screened.length === 0) return [];
  return providerColumnGroup({
    providers: screened,
    id: "screen",
    groupLabel: "Growth screen (360°, upper bound)",
    groupTitle:
      "Upper bound on 360° pictures in or around each city, from the weekly growth screen — " +
      "NOT coverage. \"0 · none\" (no imagery of any kind) is conclusive; \"0 · flat only\" " +
      "means no 360° imagery but some flat pictures; a positive number only means a closer " +
      "look is worth taking. Sorted descending, the top rows are the cities most worth " +
      "MEASURING, not the best covered.",
    keyFor: (p) => `screen_${p}`,
    cellFor: (p) => (row) => gridScreenCellParts(row, p, screenDoc.providers[p]),
    initial: "desc",
  });
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
 *   The render path passes the ones the payload actually contains; the default
 *   is the whole registry, which is what a caller with no payload wants.
 * @param {?Object} [screenDoc] - Parsed provider_screen.json.gz. Null (the
 *   default) builds exactly the columns the page had before #349.
 * @returns {Object[]}
 */
function buildGridColumns(providers = gridProviders(), screenDoc = null) {
  const pair = gridDeltaPair(providers);
  const [ahead, behind] = pair ?? [];
  const pairNames = pair ? `${providerShortLabel(ahead)} − ${providerShortLabel(behind)}` : "";

  return [
    {
      key: "label",
      label: "City",
      type: "text",
      initial: "asc",
      always: true,
      cell: gridLabelCellHtml,
    },
    ...providerColumnGroup({
      providers,
      id: "cov",
      groupLabel: "Grid coverage (%)",
      groupTitle: "Share of the city's grid sample points with a 360° panorama",
      keyFor: (p) => `pct_${p}`,
      // Compact cells: one coverage cell PER PROVIDER plus a Δ, and at the
      // full 128px that group alone would not fit the measure.
      cellFor: (p) => (row) => coverageCellParts(row[`pct_${p}`], { compact: true }),
      linkFor: gridProviderLink,
      initial: "desc",
      unit: "%",
      digits: 1,
      delta: pair && {
        key: "deltaPct",
        unit: " pp",
        title: `${pairNames}, in percentage points. Positive means Mapillary has more 360° coverage.`,
      },
    }),
    ...providerColumnGroup({
      providers,
      id: "covAny",
      groupLabel: "Any imagery (%)",
      // Says what the group MEASURES, not who is in it: the leaves below carry
      // the per-provider half, because this string is also each leaf's default
      // tooltip and naming one provider in it mislabels the others (#295).
      groupTitle:
        "Share of grid sample points with imagery of ANY kind — 360° panoramas plus flat/" +
        "perspective images, for a provider that publishes both",
      keyFor: (p) => `pctAny_${p}`,
      // Read off `hasFlatImagery` rather than naming a provider, through the
      // helper streets.js also calls so the two pages cannot drift. KartaView
      // is the case that made this matter: its flat imagery is the DOMINANT
      // half of its data (Yogyakarta, 1,071,155 flat images against 16,913
      // panos — 2.1% 360° coverage against 16.3% any-imagery), and it
      // inherited a tooltip crediting the flat column to Mapillary.
      leafTitle: (p) => anyImageryLeafTitle(p, "Equals grid coverage"),
      cellFor: (p) => (row) => coverageCellParts(row[`pctAny_${p}`], { compact: true }),
      linkFor: gridProviderLink,
      initial: "desc",
      unit: "%",
      digits: 1,
      delta: pair && {
        key: "deltaPctAny",
        unit: " pp",
        title: `${pairNames}, in percentage points. Positive means Mapillary has more imagery of any kind.`,
      },
    }),
    ...providerColumnGroup({
      providers,
      id: "age",
      groupLabel: "Median age (yrs)",
      groupTitle: "Median age of the city's panoramas at that provider's latest snapshot",
      keyFor: (p) => `medianAge_${p}`,
      cellFor: (p) => (row) => ({
        html: row[`medianAge_${p}`] == null ? "—" : formatCellNumber(row[`medianAge_${p}`], 1),
      }),
      linkFor: gridProviderLink,
      initial: "asc",
      unit: " yrs",
      digits: 1,
      delta: pair && {
        key: "deltaMedianAge",
        unit: " yrs",
        title: `${pairNames}, in years. NEGATIVE means Mapillary is fresher.`,
      },
    }),
    // No Δ here, ever: GSV samples the nearest pano per grid point while
    // Mapillary is a census of every 360° pano, so the two counts answer
    // different questions and their difference answers none.
    ...providerColumnGroup({
      providers,
      id: "panos",
      groupLabel: "Panoramas (per provider — not comparable)",
      // Was an enumeration ("...for GSV, ...for Mapillary") that simply went
      // stale when a third provider was collected. States the rule instead, so
      // it stays true however many providers the payload carries (#295).
      groupTitle:
        "Unique panoramas in each provider's latest snapshot, counted that provider's own way. " +
        "NOT comparable across providers (a sample and a census answer different questions), " +
        "which is why this group has no Δ column.",
      keyFor: (p) => `panos_${p}`,
      leafLabel: (p) => {
        const model = PROVIDERS[p]?.panoCountingModel;
        return model ? `${providerShortLabel(p)} (${model})` : providerShortLabel(p);
      },
      leafTitle: (p) => {
        const model = PROVIDERS[p]?.panoCountingModel;
        const label = PROVIDERS[p]?.label ?? p;
        let title;
        if (model === "sample") {
          title = `${label}: the nearest panorama at each grid point, so the count is bounded ` +
            "by the grid rather than by the imagery";
        } else if (model === "census") {
          title = `${label}: every 360° panorama found in the search area, so the count is ` +
            "unbounded by the grid — do not compare it against a sampled count";
        } else {
          title = `Unique panoramas in ${label}'s latest snapshot`;
        }
        // The copyright half, which the counting model does NOT imply and
        // which nothing else on the row discloses: for a copyright-filtered
        // provider, adaptCityRecord resolves `pano_count` to the
        // official-fleet subset (`unique_google_panos`) while the coverage
        // columns beside it count every PRESENT point regardless of
        // copyright. Two numbers with different denominators sat side by side
        // in one row, and the group title that used to say so ("official
        // © Google panos for GSV") was the enumeration removed above (#296
        // review). An archival run that never captured a copyright field
        // falls back to all panos, which this simplifies over exactly as the
        // title it replaces did.
        return PROVIDERS[p]?.hasCopyrightFilter
          ? `${title}. Counts official-fleet panoramas only — contributor imagery is ` +
            "excluded here but still counted in the coverage columns, so the two have " +
            "different denominators."
          : title;
      },
      cellFor: (p) => (row) => ({ html: formatCellNumber(row[`panos_${p}`]) }),
      linkFor: gridProviderLink,
      initial: "desc",
    }),
    // Frozen-grid facts, shared by every provider of a city (the geometry is a
    // city property — that is what makes the coverage rates comparable), so
    // they collapse to one column rather than repeating per provider.
    {
      key: "searchPoints",
      label: "Grid points",
      type: "number",
      initial: "desc",
      title: "Sample points in the frozen grid — the denominator Grid coverage is a share of",
      cell: (r) => `<td>${formatCellNumber(r.searchPoints)}</td>`,
    },
    {
      key: "gridWidthM",
      label: "Grid size",
      type: "number",
      initial: "desc",
      unit: " m",
      title:
        "Extent of the frozen sampling grid, width × height. Sorts by width. Oversized city " +
        "grids are capped at 40 km per side (issue #166).",
      cell: (r) => `<td>${r.gridSpanLabel ?? "—"}</td>`,
    },
    {
      key: "gridStepM",
      label: "Grid step",
      type: "number",
      initial: "asc",
      unit: " m",
      title: "Spacing between grid sample points",
      cell: (r) => `<td>${r.gridStepM == null ? "—" : `${formatCellNumber(r.gridStepM)} m`}</td>`,
    },
    {
      key: "areaKm2",
      label: "Grid area",
      type: "number",
      initial: "desc",
      unit: " km²",
      digits: 1,
      title: "Area covered by the frozen sampling grid",
      cell: (r) => `<td>${r.areaKm2 == null ? "—" : `${formatCellNumber(r.areaKm2, 1)} km²`}</td>`,
    },
    ...providerColumnGroup({
      providers,
      id: "collected",
      groupLabel: "Last collected",
      groupTitle: "Date of each provider's latest collection run — how fresh that series is",
      keyFor: (p) => `collected_${p}`,
      cellFor: (p) => (row) => ({
        html: row[`collected_${p}`] == null ? "—" : escapeHtml(row[`collected_${p}`]),
      }),
      linkFor: gridProviderLink,
      // Dates, which compare as text.
      type: "text",
      initial: "desc",
    }),
    ...providerColumnGroup({
      providers,
      id: "snapshots",
      groupLabel: "Snapshots",
      groupTitle:
        "Number of dated collection runs per provider; repeat runs enable change tracking over time",
      keyFor: (p) => `snapshots_${p}`,
      cellFor: (p) => (row) => ({ html: formatCellNumber(row[`snapshots_${p}`]) }),
      linkFor: gridProviderLink,
      initial: "desc",
    }),
    // LAST, so every column before it keeps the position it had; with no
    // screen this contributes nothing.
    ...gridScreenColumns(screenDoc),
  ];
}

/**
 * The full-registry build: what the page would render if every registered
 * provider had published something. The static vocabulary — `?sort=` keys,
 * `?cols=` names — is taken from here, and it is the default for callers that
 * have no payload. What actually renders is built per payload in
 * `renderGridRuns`, from the providers the payload contains.
 */
const GRID_COLUMNS = buildGridColumns();

/**
 * Every leaf key of one grouped metric, in table order.
 *
 * @param {string} id - Group id.
 * @param {Object[]} [columns] - The build to read; defaults to full-registry.
 * @param {Object} [opts]
 * @param {boolean} [opts.includeDelta=true] - Keep the group's Δ leaf. The
 *   default view asks for `false`; every explicitly chosen preset keeps it.
 *   Keyed on the `isGroupDelta` flag `providerColumnGroup` stamps, never on
 *   the "Δ" label — sniffing the glyph would tie a column rule to a
 *   character. Mirrors `streetGroupKeys` in streets.js.
 */
function gridGroupKeys(id, columns = GRID_COLUMNS, { includeDelta = true } = {}) {
  return columns
    .filter((c) => c.group?.id === id && (includeDelta || c.isGroupDelta !== true))
    .map((c) => c.key);
}

/**
 * Column presets. The first is the default.
 *
 * It no longer has to fit the page's content measure (#350): at four
 * collected providers nothing that carries coverage, imagery age AND the
 * collection date fits 1100px, and "Last collected" is not optional — so the
 * table scrolls inside its wrap and the city column is pinned
 * (data-table.css) to keep a scrolled row readable.
 *
 * Built from a column list rather than fixed, so a preset naming a grouped
 * metric lists exactly the leaves that were built — three providers' worth
 * when three are collected, one when one is.
 *
 * @param {Object[]} [columns] - The build to draw leaf keys from.
 * @returns {Object[]}
 */
function buildGridPresets(columns = GRID_COLUMNS) {
  const groupKeys = (id) => gridGroupKeys(id, columns);
  const metricKeys = (id) => gridGroupKeys(id, columns, { includeDelta: false });
  return [
    // The default. Its width grows with the number of COLLECTED providers,
    // and since #350 it is allowed to: "Last collected" had been the group
    // that gave way at three (#334), so at production's four providers this
    // page showed no collection date at all and the only route to one was the
    // Provenance preset or the column picker. When a city was last scraped is
    // not an optional column, and there is no width to find -- a pivoted leaf
    // is as wide as the PROVIDER NAME in its header. The table scrolls inside
    // its wrap instead, with the city column pinned (data-table.css).
    withPresetTitle(
      {
        id: "overview",
        label: "Overview",
        // Assembled from `titleParts` rather than spelled as one string -- a
        // fixed title is an enumeration, and this one promised "how fresh it
        // is" at the provider count where the age group gave way. Clause
        // order is this map's key order.
        titleLead: "The headline read:",
        titleParts: {
          cov: "how much imagery a city has",
          age: "how fresh it is",
          collected: "when it was last collected",
        },
        // Δ leaves stay out, the same call streets.html makes: a Δ compares
        // two NAMED providers while the same column spent on a metric group
        // carries every one of them. Retiring the trim would otherwise have
        // silently added three Δs this page has not shown since #334.
        // "Compare providers" and the picker still have them.
        columns: [...metricKeys("cov"), ...metricKeys("age"), ...metricKeys("collected")],
      },
      columns
    ),
    {
      id: "compare",
      label: "Compare providers",
      title: "Every head-to-head metric at once, each with its signed difference",
      columns: [...groupKeys("cov"), ...groupKeys("covAny"), ...groupKeys("age")],
    },
    {
      id: "grid",
      label: "Grid geometry",
      title: "What the percentage is a percentage OF (aggregate schema v3, issue #189)",
      columns: [
        ...groupKeys("cov"),
        "searchPoints",
        "gridWidthM",
        "gridStepM",
        "areaKm2",
      ],
    },
    {
      id: "provenance",
      label: "Provenance",
      title: "When each series was collected, how many times, and how much it holds",
      columns: [
        ...groupKeys("collected"),
        ...groupKeys("snapshots"),
        ...groupKeys("panos"),
      ],
    },
    // Only when a screen was built into `columns`. Never the default: the
    // screen is a hint, not a measure. Beside the coverage a collection run
    // MEASURED and when that was, so "worth measuring" reads against "already
    // measured" in one row.
    ...(groupKeys("screen").length > 0
      ? [
          {
            id: "screen",
            label: "Where to look next",
            title:
              "The growth screen's upper bounds beside measured coverage: \"0 · none\" rules a " +
              "city out, a positive bound only says it is worth collecting",
            columns: [...groupKeys("screen"), ...metricKeys("cov"), ...groupKeys("collected")],
          },
        ]
      : []),
  ];
}

/**
 * Filters offered in the sidebar.
 *
 * @param {string[]} [providers] - Providers to offer as scopes, in order.
 *   The render path passes the ones the payload contains; the default is the
 *   whole registry.
 * @param {string[]} [screened] - Providers the screen document carries; one
 *   select each. Empty (the default) adds none.
 * @returns {Object[]}
 */
function buildGridFilters(providers = gridProviders(), screened = []) {
  return [
    {
      key: "provider",
      // Renamed from "Provider" now that a row is a city rather than a series:
      // the question is which providers collected THIS city, not which series
      // this row is.
      label: "Collected by",
      type: "select",
      anyLabel: "Any provider",
      // One option per COLLECTED provider, plus the arity option that replaced
      // the old "Multiple providers" checkbox — with the pivot, "collected by
      // 2+ providers" is exactly "this row's Δ columns are populated", which is
      // what the checkbox was really asking.
      //
      // Collected rather than registered (issue #225 registered KartaView but
      // deliberately did not schedule it): an option matching zero rows is bad
      // enough on its own, but this select is also a SCOPE, so choosing it
      // would point every numeric slider at an all-null field — and an empty
      // domain falls back to the descriptor's `min`/`max`, i.e. an arbitrary
      // 0–1 axis on the age filter. A stale `?provider=` naming an uncollected
      // provider is simply not in `options`, and parseTableState drops a value
      // that no option offers, so such a link degrades to unscoped.
      options: providers
        .map((value) => ({ value, label: PROVIDERS[value].label }))
        .concat([{ value: SCOPE_MULTI, label: "2+ providers" }]),
      // The `?provider=gsv` links from before the pivot keep working: the value
      // vocabulary is unchanged apart from the addition.
      test: (row, value) =>
        value === SCOPE_MULTI ? row.providers.length > 1 : row.providers.includes(value),
    },
    // The numeric filters follow the scope above: pick a provider and they read
    // THAT provider's column and say so; leave it on "any" and they read the
    // best across a city's providers, with a label that spells the quantifier
    // out. `field`/`label` are the unscoped defaults, used before the first
    // resolve and by anything reading the descriptors statically.
    {
      key: "cov",
      label: "Grid coverage %",
      type: "histogram-range",
      field: "pctBest",
      min: 0,
      max: 100,
      unit: "%",
      digits: 1,
      ...scopedNumericFilter({
        base: "pct",
        bestField: "pctBest",
        label: "Grid coverage %",
        anyLabel: "any provider reaches",
      }),
    },
    {
      key: "age",
      label: "Median age (yrs)",
      type: "histogram-range",
      field: "medianAgeBest",
      min: 0,
      unit: " yrs",
      digits: 1,
      ...scopedNumericFilter({
        base: "medianAge",
        bestField: "medianAgeBest",
        label: "Median age (yrs)",
        // Unscoped, "best" is the MINIMUM — the freshest imagery any provider
        // has — so the quantifier has to be named rather than left as "best".
        anyLabel: "freshest of any",
      }),
    },
    // The head-to-head brush, and the reason the pivot exists: "show me the
    // cities where Mapillary is ahead by 20 points or more". Deliberately NOT
    // scoped: a difference is a question about the pair, so there is no single
    // provider whose column it could read instead.
    ...(gridDeltaPair(providers)
      ? [
          {
            key: "dcov",
            label: "Δ coverage (pp)",
            type: "histogram-range",
            field: "deltaPct",
            unit: " pp",
            digits: 1,
          },
        ]
      : []),
    // One per screened provider (issue #349). A select, not a slider: the
    // screen's number is a bound, and a window over bounds ("between 100 and
    // 500") asks a question the instrument cannot answer. The readings it CAN
    // answer are a fact (no imagery), flat imagery only, a 360° hint, and
    // "not looked at" -- the same four states the cell renders.
    ...screened.map((p) => ({
      key: `screen_${p}`,
      label: `Screened (${providerShortLabel(p)})`,
      type: "select",
      anyLabel: "Any",
      options: [
        { value: "none", label: "No imagery at all (0 · none)" },
        { value: "flat", label: "Flat imagery only (0 · flat only)" },
        { value: "hint", label: "360° positive (upper bound)" },
        { value: "unscreened", label: "Not screened" },
      ],
      test: (row, value) =>
        value === "unscreened"
          ? row[`screenVerdict_${p}`] == null
          : row[`screenVerdict_${p}`] === value,
    })),
  ];
}

/** The full-registry builds, for the static vocabulary and for the tests. */
const GRID_PRESETS = buildGridPresets();
const GRID_FILTERS = buildGridFilters();

/** Row fields the free-text search box looks at. */
const GRID_SEARCH_FIELDS = ["label", "cityId", "fullLabel", "providersLabel"];

/** Default sort: alphabetical, so the page opens as a browsable index. */
const GRID_DEFAULT_SORT = { key: "label", dir: "asc" };

// ── Row model ─────────────────────────────────────────────────

/**
 * a − b, or null unless BOTH operands are present.
 *
 * Null-unless-both is the whole contract: treating a missing operand as zero
 * would turn "this city has no Mapillary run" into "Mapillary is 75 points
 * behind", which is a made-up comparison, and it would then sort as one.
 */
function deltaOf(a, b) {
  return a == null || b == null ? null : a - b;
}

/**
 * The best (max, or min when `lowest`) of a row's per-provider values.
 *
 * @param {Object} row
 * @param {string[]} providers - Providers present in the payload.
 * @param {(provider: string) => string} keyFor
 * @param {boolean} [lowest] - Take the minimum instead.
 */
function bestAcrossProviders(row, providers, keyFor, lowest = false) {
  const values = providers
    .map((p) => row[keyFor(p)])
    .filter((v) => typeof v === "number" && Number.isFinite(v));
  if (values.length === 0) return null;
  return lowest ? Math.min(...values) : Math.max(...values);
}

/**
 * Pivot the aggregate into one row per CITY, with per-provider sub-fields.
 *
 * The city set is the UNION across providers, never the intersection:
 * `adaptCitiesPayload` drops a city that has no runs for the provider it is
 * adapting for, so intersecting would silently hide every single-provider
 * city — which is most of them.
 *
 * Per-provider keys are generated from the COLLECTED providers (`pct_gsv`,
 * `pct_mapillary`, …) so the row model and the columns fan out from one list.
 * A registered provider that this payload carries no cities for is dropped
 * from that list and gets no keys, which is what keeps the columns, the
 * presets and the scope options free of a provider with nothing to show — see
 * `gridProviders`. Frozen-grid geometry is a CITY property shared by every
 * provider — that is what makes their coverage rates comparable in the first
 * place — so it collapses to a single field, taken from the first provider
 * that reports it.
 *
 * A screen document (issue #349) adds, for each SCREENED provider `p`,
 * `screen_${p}` (the 360° upper bound), `screenAny_${p}` (the any-imagery
 * upper bound), `screenDate_${p}`, `screenVerdict_${p}` ("none" / "flat" /
 * "hint") and `screenFirstPositive_${p}`, all null for a city that provider
 * never screened. With no document, no key is
 * added at all, so the rows are exactly what they were before #349.
 *
 * Rows are still the cities the AGGREGATE carries. A city the screen covers
 * that has no published run is not a row — there is nothing of it to link to
 * or measure against — and is counted in `screenUnlisted` instead, so the
 * caption can say how many the table cannot show.
 *
 * @param {?Object} rawCities - Parsed cities.json.gz, or null.
 * @param {?Object} [screenDoc] - Parsed provider_screen.json.gz, or null.
 * @returns {{rows: Object[], generatedAt: ?string, providers: string[],
 *            screened: string[], screenUnlisted: Object<string, number>}}
 *   `providers` is registry order, narrowed to those the payload contains;
 *   `screened` is the screen document's own provider list.
 */
function pivotGridRows(rawCities, screenDoc = null) {
  const byCity = new Map();
  let generatedAt = null;

  // One adaptation pass per provider: a v3 record holds an independent run
  // series per provider, so each pass yields that provider's cities only.
  // Adapt first and build rows second, so the collected set is known before
  // any row is shaped — a provider is "collected" iff it yielded a city.
  const adapted = [];
  for (const provider of gridProviders()) {
    const { meta, cities } = adaptCitiesPayload(rawCities, provider);
    generatedAt ??= meta.generatedAt;
    if (cities.length > 0) adapted.push([provider, cities]);
  }
  const providers = adapted.map(([provider]) => provider);

  // Folding is keyed on city_id, so a record without one cannot be folded with
  // anything — including another record without one. Sharing a "" key merged
  // unrelated cities into a single "Unknown" row and made the catalog look
  // smaller than it is; each gets its own row instead. Latent today (the
  // published v3 aggregate always carries an id) and deliberately LOUD rather
  // than absorbed, since a record without one is a bug upstream.
  let anonymous = 0;

  for (const [provider, cities] of adapted) {
    for (const city of cities) {
      if (city.city_id == null) {
        console.warn("grid: aggregate record with no city_id gets its own row", city);
      }
      const cityId = city.city_id ?? "";
      const key = city.city_id ?? `\u0000anonymous-${(anonymous += 1)}`;
      let row = byCity.get(key);
      if (!row) {
        row = {
          cityId,
          label: city.city_id ? cityDisplayLabel(city) : "Unknown",
          // The spelled-out label, kept beside the abbreviated one so the
          // tooltip and the free-text search can both still reach "Indiana"
          // and "United States" (GRID_SEARCH_FIELDS reads both).
          fullLabel: city.city_id ? cityFullLabel(city) : "Unknown",
          providers: [],
          providersLabel: "",
          providerCount: 0,
          // The Δ keys always exist, so "this payload has no Δ pair" reads as
          // null rather than as a missing field — the same null-unless-both
          // contract, extended to "the pair was not collected here at all".
          deltaPct: null,
          deltaPctAny: null,
          deltaMedianAge: null,
          searchPoints: null,
          gridWidthM: null,
          gridStepM: null,
          gridSpanLabel: null,
          areaKm2: null,
          filename: null,
        };
        for (const p of providers) {
          row[`pct_${p}`] = null;
          row[`pctAny_${p}`] = null;
          row[`medianAge_${p}`] = null;
          row[`panos_${p}`] = null;
          row[`collected_${p}`] = null;
          row[`snapshots_${p}`] = null;
          row[`filename_${p}`] = null;
        }
        byCity.set(key, row);
      }

      row.providers.push(provider);
      row[`pct_${provider}`] = city.coverage_rate_percent ?? null;
      // Any-imagery coverage (issue #116): Mapillary's full footprint
      // including flat imagery; the adapter falls back to the 360° rate for
      // GSV/pre-v7.
      row[`pctAny_${provider}`] = city.any_imagery_coverage_rate_percent ?? null;
      row[`medianAge_${provider}`] = city.pano_age_stats?.median_pano_age_years ?? null;
      row[`panos_${provider}`] = city.pano_count ?? null;
      row[`collected_${provider}`] = city.latest_run_date ?? null;
      row[`snapshots_${provider}`] = (city.runs ?? []).length || null;
      row[`filename_${provider}`] = city.data_file?.filename ?? null;

      // Shared frozen-grid facts: first provider to report each one wins. They
      // describe the city, not the series, so a later provider cannot
      // contradict them — and a provider whose record predates schema v3
      // carries nulls that must not overwrite a real value.
      const width = city.grid?.width_meters ?? null;
      const height = city.grid?.height_meters ?? null;
      row.searchPoints ??= city.total_search_points ?? null;
      row.gridWidthM ??= width;
      row.gridStepM ??= city.grid?.step_length_meters ?? null;
      row.areaKm2 ??= city.search_area_km2 ?? null;
      row.gridSpanLabel ??=
        width == null || height == null
          ? null
          : `${formatCellNumber(width / 1000, 1)} × ${formatCellNumber(height / 1000, 1)} km`;
      row.filename ??= city.data_file?.filename ?? null;
    }
  }

  const pair = gridDeltaPair(providers);
  const rows = [...byCity.values()];
  for (const row of rows) {
    row.providerCount = row.providers.length;
    row.providersLabel = row.providers.map(providerShortLabel).join(", ");
    if (pair) {
      const [a, b] = pair;
      row.deltaPct = deltaOf(row[`pct_${a}`], row[`pct_${b}`]);
      row.deltaPctAny = deltaOf(row[`pctAny_${a}`], row[`pctAny_${b}`]);
      row.deltaMedianAge = deltaOf(row[`medianAge_${a}`], row[`medianAge_${b}`]);
    }
    row.pctBest = bestAcrossProviders(row, providers, (p) => `pct_${p}`);
    // The freshest, i.e. the MINIMUM age — "best" here is the small number.
    row.medianAgeBest = bestAcrossProviders(row, providers, (p) => `medianAge_${p}`, true);
  }

  const screened = screenedProviders(screenDoc);
  const screenUnlisted = {};
  for (const p of screened) {
    for (const row of rows) {
      // An id-less row joins nothing (its cityId is ""), so it reads unscreened.
      const record = row.cityId ? lookupScreen(screenDoc, p, row.cityId) : null;
      row[`screen_${p}`] = record?.pictures_360_upper_bound ?? null;
      row[`screenAny_${p}`] = record?.pictures_upper_bound ?? null;
      row[`screenDate_${p}`] = record?.screen_date ?? null;
      row[`screenVerdict_${p}`] = screenVerdict(record);
      // Absent (never positive) in the artifact reads null here.
      row[`screenFirstPositive_${p}`] = record?.first_positive_date ?? null;
    }
    screenUnlisted[p] = screenDoc.providers[p].cities.filter(
      (record) => !byCity.has(record?.city_id)
    ).length;
  }
  return { rows, generatedAt, providers, screened, screenUnlisted };
}

/**
 * Build one table row from a row model.
 *
 * @param {Object} row - From pivotGridRows.
 * @param {Object[]} [columns] - Visible columns; defaults to all of them.
 * @returns {string} HTML for one <tr>.
 */
function gridRowHtml(row, columns = GRID_COLUMNS) {
  return rowHtmlFromColumns(columns, row);
}

// The table + controls controllers (created on first render so a header click
// or a filter change can repaint without refetching or re-adapting).
let gridTable = null;
let gridControls = null;

/**
 * Render the table (or the empty state) from the aggregate payload.
 *
 * @param {?Object} rawCities - Parsed cities.json.gz, or null.
 * @param {?Object} [screenDoc] - Parsed provider_screen.json.gz, or null.
 */
function renderGridRuns(rawCities, screenDoc = null) {
  const statusEl = document.getElementById("grid-status");
  const wrapEl = document.getElementById("grid-table-wrap");

  const { rows, generatedAt, providers, screened, screenUnlisted } = pivotGridRows(
    rawCities,
    screenDoc
  );

  if (rows.length === 0) {
    statusEl.textContent = "No city collections have been published yet.";
    return;
  }

  // Built from the providers THIS payload carries, not from the registry: a
  // registered-but-uncollected provider would otherwise contribute a leaf to
  // every metric group (six more columns and three more default-preset ones
  // for KartaView alone), all of them em-dashes, plus a scope option that
  // matches no rows and points every slider at an all-null field.
  const columns = buildGridColumns(providers, screenDoc);
  gridTable ??= createSortableTable({
    columns,
    defaultSort: GRID_DEFAULT_SORT,
    theadEl: document.getElementById("grid-thead"),
    tbodyEl: document.getElementById("grid-tbody"),
  });
  gridControls ??= createTableControls({
    rootEl: document.getElementById("grid-controls"),
    table: gridTable,
    columns,
    presets: buildGridPresets(columns),
    filters: buildGridFilters(providers, screened),
    searchFields: GRID_SEARCH_FIELDS,
    onChange: (shown, all) =>
      updateGridCaption(shown, all, generatedAt, gridScreenCaption(screenDoc, screenUnlisted)),
  });
  gridControls.setRows(rows);
  renderGridScreenSections(screenDoc);

  statusEl.hidden = true;
  wrapEl.hidden = false;
}

/**
 * Keep the caption reporting what is actually on screen.
 *
 * Two counts, because a row is now a city while the underlying data is still
 * per-provider series: "1,214 cities" alone would understate the collection by
 * roughly a third, and "1,501 series" alone would no longer describe the rows.
 *
 * @param {Object[]} shown - Filtered rows.
 * @param {Object[]} all - Every row.
 * @param {?string} generatedAt - Aggregate timestamp.
 * @param {string} [screenSuffix] - From gridScreenCaption; "" with no screen.
 */
function updateGridCaption(shown, all, generatedAt, screenSuffix = "") {
  const series = shown.reduce((n, row) => n + row.providerCount, 0);
  const noun = `${formatCellNumber(series)} provider series`;
  const counts =
    shown.length === all.length
      ? `${formatCellNumber(all.length)} cities (${noun})`
      : `${formatCellNumber(shown.length)} of ${formatCellNumber(all.length)} cities (${noun})`;
  document.getElementById("grid-caption").textContent =
    counts +
    (generatedAt ? ` · updated ${new Date(generatedAt).toLocaleString()}` : "") +
    screenSuffix;
}

/**
 * The caption's growth-screen clause, one per screened provider, or "".
 *
 * Read off the LAST `series` point — the latest screen's own catalog-level
 * counts — rather than recounted from the city records, which carry each
 * city's latest answer and so can mix screen dates. "Screened positive" and
 * not "hold imagery": a positive bound is a hint. "(any imagery)" because
 * `cities_positive` counts `pictures_upper_bound > 0`, which is the column's
 * "flat only" and "≤ N" cells together, not the 360° hints alone.
 *
 * The unlisted count is its own clause, not "N of them": it counts screened
 * records of ANY verdict (zeros included) and of any screen date, so it is
 * not a subset of the positive count beside it.
 *
 * @param {?Object} screenDoc - Parsed provider_screen.json.gz, or null.
 * @param {Object<string, number>} [unlisted] - From pivotGridRows.
 * @returns {string}
 *
 * @example
 *   gridScreenCaption(doc, { panoramax: 2 });
 *   // " · Panoramax screen 2026-09-16: 411 of 1,221 cities screened positive
 *   //    (any imagery); 2 screened cities have no published run, so no row here"
 */
function gridScreenCaption(screenDoc, unlisted = {}) {
  return screenedProviders(screenDoc)
    .map((p) => {
      const entry = screenDoc.providers[p];
      const series = Array.isArray(entry.series) ? entry.series : [];
      const last = series[series.length - 1];
      if (!last) return "";
      const n = unlisted[p];
      const missing = n
        ? `; ${formatCellNumber(n)} screened ${n === 1 ? "city has" : "cities have"} ` +
          "no published run, so no row here"
        : "";
      return (
        ` · ${providerShortLabel(p)} screen ${entry.latest_screen_date ?? last.screen_date}: ` +
        `${formatCellNumber(last.cities_positive)} of ${formatCellNumber(last.cities_screened)} ` +
        `cities screened positive (any imagery)${missing}`
      );
    })
    .join("");
}

/**
 * The series disclosure for one screened provider: a plain table, one row per
 * screen date. No chart library is loaded on this page, and a handful of
 * weekly points reads fine as rows.
 *
 * @param {string} provider
 * @param {Object} entry - `doc.providers[provider]`.
 * @returns {string} HTML for one <details>.
 */
function gridScreenSeriesHtml(provider, entry) {
  const name = escapeHtml(providerShortLabel(provider));
  const series = Array.isArray(entry?.series) ? entry.series : [];
  const body = series
    .map(
      (point) =>
        `<tr><th scope="row">${escapeHtml(point.screen_date ?? "—")}</th>` +
        `<td>${formatCellNumber(point.cities_screened)}</td>` +
        `<td>${formatCellNumber(point.cities_positive)}</td>` +
        `<td>${formatCellNumber(point.pictures_360_upper_bound)}</td></tr>`
    )
    .join("");
  return `
    <details class="screen-series" data-provider="${escapeHtml(provider)}">
      <summary>${name} screen over time</summary>
      <table class="screen-series-table">
        <caption>
          Every ${name} growth screen since ${escapeHtml(entry?.first_screen_date ?? "—")}.
          The sum is an upper bound that double-counts imagery shared by
          neighbouring cities: a trend line, never an inventory.
        </caption>
        <thead><tr>
          <th scope="col">Screen date</th>
          <th scope="col">Cities screened</th>
          <th scope="col">Cities positive (any imagery)</th>
          <th scope="col">360° upper bound (sum)</th>
        </tr></thead>
        <tbody>${body}</tbody>
      </table>
    </details>`;
}

/**
 * Show the screen's prose and series sections, or leave them hidden.
 *
 * Both are authored `hidden` in grid.html, so a page with no screen document
 * renders exactly as it did before #349.
 *
 * @param {?Object} screenDoc - Parsed provider_screen.json.gz, or null.
 */
function renderGridScreenSections(screenDoc) {
  const screened = screenedProviders(screenDoc);
  if (screened.length === 0) return;
  const seriesEl = document.getElementById("grid-screen-series");
  const aboutEl = document.getElementById("grid-screen-about");
  if (seriesEl) {
    seriesEl.innerHTML = screened
      .map((p) => gridScreenSeriesHtml(p, screenDoc.providers[p]))
      .join("");
    seriesEl.hidden = false;
  }
  if (aboutEl) aboutEl.hidden = false;
}

/**
 * Fetch the aggregate and the provider screen concurrently, then render.
 *
 * `fetchProviderScreen` resolves null rather than rejecting, so a missing
 * screen cannot fail the page — it only leaves the screen sections out.
 */
async function loadGridRuns() {
  try {
    const [rawCities, screenDoc] = await Promise.all([
      fetchGzippedJson(STREETSCAPE_DATA_BASE_URL + "cities.json.gz"),
      fetchProviderScreen(),
    ]);
    renderGridRuns(rawCities, screenDoc);
  } catch (error) {
    console.error("Error loading grid coverage:", error);
    document.getElementById("grid-status").textContent =
      "Error loading grid coverage. Please check the console for details.";
  }
}

// Guarded so `require`ing this file in the Node unit tests (which have no
// document) exercises the pure helpers without trying to load anything.
if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", loadGridRuns);
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    pivotGridRows,
    gridRowHtml,
    gridDeltaPair,
    gridProviders,
    buildGridColumns,
    buildGridPresets,
    buildGridFilters,
    renderGridRuns,
    updateGridCaption,
    gridScreenCaption,
    gridScreenColumns,
    gridScreenCellParts,
    gridScreenSeriesHtml,
    GRID_COLUMNS,
    GRID_PRESETS,
    GRID_FILTERS,
    GRID_SEARCH_FIELDS,
    GRID_DEFAULT_SORT,
    GRID_DELTA_PAIRS,
  };
}
