/**
 * driving.js — the Driving page (driving.html).
 *
 * Joins Google's published Street View driving plan against the imagery this
 * project has actually observed, one row per tracked city, from
 * driving_plan.json.gz (issue #176).
 *
 * The page exists because neither source is trustworthy alone. Google's feed
 * records ANNOUNCED campaigns and keeps stale rows indefinitely: its Israeli
 * entries all read publish=No with 2018-19 windows, while our own runs record
 * captures in 2023-09 and 2023-10. Google drove Israel four years after the
 * feed said the campaign closed and never revised it. So an absent or closed
 * plan row is not evidence a city was not driven — the `driven_unplanned`
 * verdict names that case outright, and the page says so in prose too.
 *
 * Rows are PLACES, of two kinds: every city we track, and every plan record
 * covering no tracked city. A "Tracked?" column only carries information if
 * untracked places are rows too, and a record that already matches tracked
 * cities is represented by those cities rather than duplicated. The union is
 * ~3,800 rows, comfortably inside what the table chassis renders (measured:
 * ~70 ms to sort + filter + build, against 28 ms for grid.html's 1,501).
 * Districts are NOT exploded into rows — 11,765 would cost ~200 ms and 8.6 MB
 * of HTML per keystroke — so they stay searchable as a joined string on their
 * record's row, which is how "Ada" finds Boise.
 *
 * Depends on globals from streetscape-utils.js (loaded first):
 * STREETSCAPE_DATA_BASE_URL, fetchGzippedJson, escapeHtml — from
 * table-utils.js: sortRowsBy, formatCellNumber, coverageCellHtml,
 * rowHtmlFromColumns, createSortableTable — and from table-controls.js:
 * createTableControls.
 */

/**
 * How each verdict reads on screen, and the tone it carries.
 *
 * `label` is deliberately plainer than the machine vocabulary: a reader
 * scanning the column should not have to learn six identifiers to use the
 * page. `hint` becomes the cell tooltip and carries the caveat that the label
 * alone cannot.
 */
const VERDICTS = {
  drive_confirmed: {
    label: "Drive confirmed",
    tone: "good",
    hint: "Imagery we observed was captured inside a plan window we archived — the drive happened.",
  },
  planned_open: {
    label: "Driving now",
    tone: "active",
    hint: "Google's published window for this area is open right now.",
  },
  planned_upcoming: {
    label: "Planned",
    tone: "active",
    hint: "Google has published a future window for this area.",
  },
  driven_unplanned: {
    label: "Driven, unplanned",
    tone: "warn",
    hint:
      "We observed imagery captured well after this area's last published window closed — " +
      "Google drove it without the feed ever saying so. The clearest evidence that the plan " +
      "under-reports.",
  },
  closed: {
    label: "Campaign closed",
    tone: "muted",
    hint:
      "No open window. This is NOT evidence the area will not be driven — see Israel, whose " +
      "rows closed in 2019 while its imagery is from 2023.",
  },
  not_listed: {
    label: "Not in plan",
    tone: "muted",
    hint:
      "No entry for this area in the archived feed. Google notes that listed regions may " +
      "include smaller places nearby, so absence is not a guarantee of no driving.",
  },
};

/**
 * Cell for the "City" column: the label, hyperlinked to the city page when the
 * row has a published run to link to.
 *
 * Same degrade-not-disappear posture as gridLabelCellHtml — a city with no
 * collected run still renders, just as plain text.
 *
 * @param {Object} row - From drivingRowModel.
 * @returns {string} HTML for one <th scope="row">.
 */
function drivingLabelCellHtml(row) {
  const label = escapeHtml(row.label);
  const content = row.filename
    ? `<a class="streets-view-link" href="city.html?file=${encodeURIComponent(row.filename)}">${label}</a>`
    : label;
  return `<th scope="row" title="${label}">${content}</th>`;
}

/**
 * Cell for the verdict column: a toned pill carrying the verdict's hint as its
 * tooltip.
 *
 * @param {Object} row - From drivingRowModel.
 * @returns {string} HTML for one <td>.
 */
function verdictCellHtml(row) {
  const spec = VERDICTS[row.verdict];
  if (!spec) return `<td>${escapeHtml(row.verdict ?? "—")}</td>`;
  return (
    `<td><span class="verdict-pill verdict-${spec.tone}" title="${escapeHtml(spec.hint)}">` +
    `${escapeHtml(spec.label)}</span></td>`
  );
}

/**
 * Cell for a date that may be approximate.
 *
 * Israel's feed dates are dirty (`14/2/19`), so the artifact recovers them with
 * a day-first reading and flags the window `window_approximate`. Marking that
 * in the cell keeps a heuristic from reading as a published fact.
 *
 * @param {?string} value - ISO date or null.
 * @param {boolean} approximate - Whether the bound was recovered, not published.
 * @returns {string} HTML for one <td>.
 */
function windowCellHtml(value, approximate) {
  if (value == null) return "<td>—</td>";
  const text = escapeHtml(value);
  return approximate
    ? `<td title="Recovered from a malformed date in Google's feed; day-first reading">${text}<span class="approx-flag" aria-hidden="true">~</span><span class="visually-hidden"> (approximate)</span></td>`
    : `<td>${text}</td>`;
}

/**
 * Cell for the drive window, rendered as a span rather than a lone date.
 *
 * "Window closes: 2026-11-01" on its own does not say what window, or since
 * when — the span does, and it fits where two columns did.
 *
 * @param {Object} row - From the row model.
 * @returns {string} HTML for one <td>.
 */
function windowRangeCellHtml(row) {
  if (row.windowStart == null && row.windowEnd == null) return "<td>—</td>";
  const start = escapeHtml(row.windowStart ?? "?");
  const end = escapeHtml(row.windowEnd ?? "?");
  const text = start === end ? start : `${start} → ${end}`;
  const approx = row.windowApproximate
    ? '<span class="approx-flag" aria-hidden="true">~</span><span class="visually-hidden"> (approximate)</span>'
    : "";
  const title = row.windowApproximate
    ? ' title="Recovered from a malformed date in Google\'s feed; day-first reading"'
    : "";
  return `<td class="window-cell"${title}>${text}${approx}</td>`;
}

/** Bars in the sparkline; enough for Street View's whole lifetime so far. */
const SPARK_BARS_MAX = 20;

/**
 * Cell for the capture-year sparkline — "when was this place actually driven".
 *
 * A median age collapses a city's whole history to one number, so a city
 * driven in 2019, 2022 and 2024 is indistinguishable from one driven once in
 * 2022. The distribution is the honest answer, and it is the only view of "the
 * past" this page can offer: per-pano capture history needs issue #2's
 * harvester, which has never been run.
 *
 * Data comes from the per-run JSON's `google_panos` block, which is already
 * filtered to official © Google imagery. That used to make it the only column
 * here immune to the corrupt third-party capture dates of issue #213; the
 * catalog's capture-date columns are now filtered the same way, so this and
 * the median-age column beside it finally describe one population.
 *
 * Bars are `aria-hidden`; the accessible content is the text summary, the same
 * split `coverageCellHtml` uses for its bar.
 *
 * @param {Object} row - From the row model.
 * @returns {string} HTML for one <td>.
 */
function sparklineCellHtml(row) {
  const years = row.captureYears;
  if (!years || !years.length) return "<td>—</td>";
  const [firstYear, counts] = years;
  if (!Array.isArray(counts) || counts.length === 0) return "<td>—</td>";

  // Keep the most recent window when a city's history is longer than the
  // strip: recency is what a reader is scanning for.
  const start = Math.max(0, counts.length - SPARK_BARS_MAX);
  const shown = counts.slice(start);
  const shownFirst = firstYear + start;
  const peak = Math.max(...shown);

  // LOG scale, not linear. Pano counts per year span four or five orders of
  // magnitude — a fresh full drive of a city is tens of thousands while an
  // older surviving pass may be a few hundred — so on a linear scale every
  // year but the newest collapses into an invisible sliver, which defeats the
  // column's whole purpose of showing repeat drives. The question here is
  // "was there a drive this year", not "how many panos exactly".
  const logPeak = Math.log10(peak + 1);
  const bars = shown
    .map((count, i) => {
      const scaled = logPeak > 0 ? (Math.log10(count + 1) / logPeak) * 100 : 0;
      const pct = count > 0 ? Math.max(12, scaled) : 0;
      const year = shownFirst + i;
      return `<span class="spark-bar${count > 0 ? "" : " spark-empty"}" style="height:${pct.toFixed(0)}%" title="${year}: ${count.toLocaleString()}"></span>`;
    })
    .join("");

  const lastYear = shownFirst + shown.length - 1;
  const busiest = shownFirst + shown.indexOf(peak);
  const label = `${shownFirst}–${lastYear}, busiest ${busiest}`;
  return (
    `<td class="spark-cell" title="${escapeHtml(label)}">` +
    `<span class="spark" aria-hidden="true">${bars}</span>` +
    `<span class="visually-hidden">${escapeHtml(label)}</span></td>`
  );
}

/**
 * Cell for the Tracked column.
 *
 * The whole reason plan areas are rows: without them this column would read
 * "yes" on every row and answer nothing.
 *
 * @param {Object} row - From the row model.
 * @returns {string} HTML for one <td>.
 */
function trackedCellHtml(row) {
  if (row.scope !== "city") {
    return '<td><span class="scope-pill scope-area" title="In Google\'s plan, but this project collects no city here">Not tracked</span></td>';
  }
  return row.enabled
    ? '<td><span class="scope-pill scope-city" title="Collected on the rolling schedule">Tracked</span></td>'
    : '<td><span class="scope-pill scope-paused" title="Registered but not currently collected">Paused</span></td>';
}

/**
 * The columns, in table order.
 *
 * Verdict first, then every metric behind it, so a reader can always re-derive
 * the verdict from the same row rather than taking it on trust.
 */
const DRIVING_COLUMNS = [
  {
    key: "label",
    label: "City",
    type: "text",
    initial: "asc",
    always: true,
    cell: drivingLabelCellHtml,
  },
  {
    key: "verdict",
    label: "Verdict",
    type: "text",
    initial: "asc",
    title:
      "Google's published plan read together with the imagery we observed. Hover a value for " +
      "what it means.",
    cell: verdictCellHtml,
  },
  {
    key: "scope",
    label: "Tracked",
    type: "text",
    initial: "asc",
    title:
      "Whether this project collects imagery here. Rows marked “Not tracked” are places named " +
      "in Google's plan that we have no city for — candidates for collection.",
    cell: trackedCellHtml,
  },
  {
    key: "country",
    label: "Country",
    type: "text",
    initial: "asc",
    title: "Country as recorded in our catalog",
    cell: (r) => `<td>${escapeHtml(r.country ?? "—")}</td>`,
  },
  {
    key: "region",
    label: "State / region",
    type: "text",
    initial: "asc",
    title: "The administrative level Google's feed keys its windows to",
    cell: (r) => `<td>${escapeHtml(r.region ?? "—")}</td>`,
  },
  {
    key: "planStatus",
    label: "Plan status",
    type: "text",
    initial: "asc",
    title:
      "Google's publish flag read together with the window: Upcoming before the window opens, " +
      "Active while it is open, Elapsed when Google still lists it as published but the window " +
      "has run out, Closed once the flag itself reads No.",
    cell: (r) => `<td>${escapeHtml(r.planStatus ?? "—")}</td>`,
  },
  // One column, not two. A bare date headed "Window closes" reads as a
  // deadline of unclear provenance when the start date is not on screen; the
  // span says what it is. Sorts on the end date, which is the urgent half.
  {
    key: "windowEnd",
    label: "Drive window",
    type: "text",
    initial: "desc",
    title:
      "The dates Google published for driving this area — when its cars are scheduled to be " +
      "there. Sorts by the closing date.",
    cell: (r) => windowRangeCellHtml(r),
  },
  {
    key: "windowStart",
    label: "Window opens",
    type: "text",
    initial: "asc",
    title: "Start of Google's published driving window, on its own",
    cell: (r) => windowCellHtml(r.windowStart, r.windowApproximate),
  },
  {
    key: "daysToWindowEnd",
    label: "Days left",
    type: "number",
    initial: "asc",
    unit: " d",
    title:
      "Days until the published window closes. Negative means it already has — which is not " +
      "the same as the area having been driven.",
    cell: (r) => `<td>${r.daysToWindowEnd == null ? "—" : formatCellNumber(r.daysToWindowEnd)}</td>`,
  },
  // Two different coverage measures, deliberately both named and never
  // conflated. Grid coverage is an AREA measure (share of sample points in a
  // lattice over the city). Street coverage is a NETWORK measure (share of
  // road-kilometres actually driven), which is the more informative answer to
  // "did Google drive here" — but it exists only for cities that have been
  // road-walked, and its denominator is different, so it is never a fallback
  // for the grid number and an unwalked city reads "no data", not 0%.
  {
    key: "coveragePct",
    label: "Grid coverage",
    type: "number",
    initial: "desc",
    unit: "%",
    title:
      "AREA measure: share of the city's grid sample points with a Google Street View panorama. " +
      "Available for every collected city.",
    cell: (r) => coverageCellHtml(r.coveragePct),
  },
  {
    key: "streetPct",
    label: "Street coverage",
    type: "number",
    initial: "desc",
    unit: "%",
    title:
      "NETWORK measure: share of the city's road-kilometres with imagery, from road-walk " +
      "collection — the closer answer to “was this actually driven”. Blank for cities not yet " +
      "walked; NOT comparable to grid coverage (different denominator).",
    cell: (r) => coverageCellHtml(r.streetPct),
  },
  {
    key: "googlePanos",
    label: "© Google panos",
    type: "number",
    initial: "desc",
    title:
      "Official © Google panoramas in the latest snapshot. Zero means every covered point is a " +
      "third-party photosphere, not a Google drive.",
    cell: (r) => `<td>${formatCellNumber(r.googlePanos)}</td>`,
  },
  {
    key: "newestCapture",
    label: "Newest capture",
    type: "text",
    initial: "desc",
    title:
      "Most recent official © Google capture date observed in the latest snapshot. Blank for a " +
      "city with no Google imagery, or where the catalog's value is impossible (a corrupt EXIF " +
      "date) and was therefore suppressed.",
    cell: (r) => `<td>${escapeHtml(r.newestCapture ?? "—")}</td>`,
  },
  {
    key: "yearsSinceCapture",
    label: "Years since",
    type: "number",
    initial: "desc",
    unit: " yrs",
    digits: 1,
    title: "Years since the newest observed capture — sort descending to find the most overdue",
    cell: (r) =>
      `<td>${r.yearsSinceCapture == null ? "—" : `${formatCellNumber(r.yearsSinceCapture, 1)} yrs`}</td>`,
  },
  {
    key: "medianAge",
    label: "Median age",
    type: "number",
    initial: "desc",
    unit: " yrs",
    digits: 1,
    title:
      "Median age of the city's official © Google panoramas. A recent newest-capture with a " +
      "high median means a partial refresh, not a full re-drive. Blank where a city's only " +
      "imagery is third-party photospheres — no Google drive to date.",
    cell: (r) =>
      `<td>${r.medianAge == null ? "—" : `${formatCellNumber(r.medianAge, 1)} yrs`}</td>`,
  },
  {
    key: "captureSpanYears",
    label: "Capture history",
    type: "number",
    initial: "desc",
    unit: " yrs",
    title:
      "Distribution of official © Google capture years — one bar per year, tallest = most " +
      "panoramas. Shows repeat drives that a median age hides. Sorts by the span covered.",
    cell: sparklineCellHtml,
  },
  {
    key: "captureDateChanged",
    label: "Refreshed panos",
    type: "number",
    initial: "desc",
    title:
      "Panoramas whose capture date changed since the previous snapshot — direct evidence that " +
      "imagery was actually refreshed, not merely planned.",
    cell: (r) => `<td>${formatCellNumber(r.captureDateChanged)}</td>`,
  },
  {
    key: "mapillaryPct",
    label: "Mapillary 360°",
    type: "number",
    initial: "desc",
    unit: "%",
    title: "Mapillary 360° panorama coverage on the same frozen grid",
    cell: (r) => coverageCellHtml(r.mapillaryPct),
  },
  {
    key: "districtCount",
    label: "Districts",
    type: "number",
    initial: "desc",
    title:
      "How many districts Google names in this plan entry — counties in the US, municipalities " +
      "or cities elsewhere. The unit the feed actually schedules at.",
    cell: (r) => `<td>${formatCellNumber(r.districtCount)}</td>`,
  },
  {
    key: "matchTier",
    label: "Match",
    type: "text",
    initial: "asc",
    title:
      "How this city was matched to Google's feed: by region (strongest), by district name, by " +
      "country only (weakest), or by a hand-written link where names could not be reconciled.",
    cell: (r) => `<td>${escapeHtml(r.matchTier ?? "—")}</td>`,
  },
  {
    key: "lastRun",
    label: "Last collected",
    type: "text",
    initial: "desc",
    title: "Date of our latest collection run for this city",
    cell: (r) => `<td>${escapeHtml(r.lastRun ?? "—")}</td>`,
  },
];

/**
 * Column presets. The first is the default and must fit the page's measure
 * without horizontal scrolling.
 */
const DRIVING_PRESETS = [
  {
    id: "overview",
    label: "Overview",
    title: "The headline read: what Google says, what we observed, and whether they agree",
    // planStatus is deliberately NOT here despite fitting the theme: it
    // largely restates the verdict (Active/Elapsed/Closed vs Driving now/
    // Campaign closed), and the width it costs pushed the table past its
    // container at real city-name lengths. It lives in the plan preset, where
    // the distinction between "published" and "elapsed" is the point.
    columns: ["verdict", "scope", "windowEnd", "coveragePct", "streetPct", "captureSpanYears"],
  },
  {
    id: "plan",
    label: "Google's plan",
    title: "What Google published, and how confidently we matched this place to it",
    columns: ["verdict", "scope", "country", "region", "planStatus", "windowEnd", "daysToWindowEnd", "districtCount", "matchTier"],
  },
  {
    id: "observed",
    label: "What we observed",
    title: "The imagery side of the join, independent of anything Google published",
    columns: ["coveragePct", "streetPct", "googlePanos", "newestCapture", "yearsSinceCapture", "medianAge", "captureDateChanged", "lastRun"],
  },
  {
    id: "history",
    label: "Capture history",
    title: "When each place was actually driven — repeat drives a median age would hide",
    columns: ["captureSpanYears", "newestCapture", "yearsSinceCapture", "medianAge", "googlePanos", "captureDateChanged"],
  },
  {
    id: "staleness",
    label: "Most overdue",
    title: "Sort by years since the newest capture to find places waiting longest for a re-drive",
    columns: ["verdict", "yearsSinceCapture", "medianAge", "newestCapture", "planStatus", "windowEnd"],
  },
];

/** Filters offered above the table. */
const DRIVING_FILTERS = [
  {
    key: "scope",
    label: "Place",
    type: "select",
    anyLabel: "Tracked and untracked",
    options: [
      { value: "city", label: "Cities we track" },
      { value: "area", label: "Plan areas we don't track" },
    ],
    test: (row, value) => row.scope === value,
  },
  {
    key: "verdict",
    label: "Verdict",
    type: "select",
    anyLabel: "All verdicts",
    options: Object.entries(VERDICTS).map(([value, spec]) => ({ value, label: spec.label })),
    test: (row, value) => row.verdict === value,
  },
  {
    key: "plan",
    label: "Plan status",
    type: "select",
    anyLabel: "Any plan status",
    options: [
      { value: "Active", label: "Window open now" },
      { value: "Upcoming", label: "Published, window not yet open" },
      { value: "Elapsed", label: "Published, window elapsed" },
      { value: "Closed", label: "Campaign closed" },
      { value: "None", label: "Not in the plan" },
    ],
    test: (row, value) => (row.planStatus ?? "None") === value,
  },
  {
    key: "cov",
    label: "Grid coverage %",
    type: "range",
    field: "coveragePct",
    min: 0,
    max: 100,
  },
  {
    key: "street",
    label: "Street coverage %",
    type: "range",
    field: "streetPct",
    min: 0,
    max: 100,
  },
  {
    key: "since",
    label: "Years since capture",
    type: "range",
    field: "yearsSinceCapture",
    min: 0,
  },
  {
    key: "enabled",
    label: "Actively collected",
    type: "boolean",
    title: "Only cities the scheduler still collects; registered-but-disabled cities are hidden",
    test: (row) => row.enabled === true,
  },
];

/** Row fields the free-text search box looks at. */
const DRIVING_SEARCH_FIELDS = ["label", "cityId", "country", "region", "districts"];

/**
 * Default sort: alphabetical by place name.
 *
 * Deliberately not verdict-first. A reader arriving from a link usually wants
 * to find one place, and the Verdict column header sorts the contradictions to
 * the top in one click for anyone who came to browse them instead.
 */
const DRIVING_DEFAULT_SORT = { key: "label", dir: "asc" };

/**
 * Google's plan status for a place, in three states rather than two.
 *
 * `publish = Yes` alone is not "active": 214 of the feed's records are still
 * flagged published with a window that closed months ago. Reporting those as
 * Active put "Plan status: Active" on the same row as the verdict "Campaign
 * closed" — a visible contradiction, and the column was the wrong half of it,
 * since the verdict correctly accounts for the elapsed window.
 *
 * "Elapsed" is deliberately its own state rather than folded into Closed:
 * Google saying "yes, published" about a window that has run out is a
 * different fact from Google saying "no", and the difference is exactly the
 * kind of feed staleness this page exists to surface.
 *
 * "Upcoming" is the symmetric case, and it has to exist for the same reason:
 * a published window that has not opened yet is not "Active" either. Reporting
 * it as Active put "Plan status: Active" beside the verdict "Planned" — which
 * plan_match.classify derives correctly as `planned_upcoming` — under a column
 * tooltip promising Active meant the window was open, and made the "Window
 * open now" filter select campaigns that have not started.
 *
 * The publish flag is read the way plan_match.is_published reads it, trimmed
 * and case-insensitively: the catalog stores Google's bytes verbatim, and a
 * stricter test here than on the Python side is how the two halves of one row
 * end up contradicting each other.
 *
 * @param {?string} publishFlag - Google's publish value, or null.
 * @param {?string} windowStart - ISO start date, or null.
 * @param {?string} windowEnd - ISO end date, or null.
 * @param {Date} today - Reference date.
 * @returns {?string} "Active", "Upcoming", "Elapsed", "Closed", or null.
 */
function planStatusFor(publishFlag, windowStart, windowEnd, today) {
  if (publishFlag == null) return null;
  if (String(publishFlag).trim().toLowerCase() !== "yes") return "Closed";
  const daysToEnd = daysUntil(windowEnd, today);
  if (daysToEnd != null && daysToEnd < 0) return "Elapsed";
  const daysToStart = daysUntil(windowStart, today);
  if (daysToStart != null && daysToStart > 0) return "Upcoming";
  return "Active";
}

/**
 * Whole days from today to an ISO date. Negative when the date has passed.
 *
 * Uses panoDateOrNull's local-midnight convention rather than Date.parse, which
 * reads a bare YYYY-MM-DD as UTC and can shift the result by a day.
 *
 * @param {?string} iso - ISO date or null.
 * @param {Date} today - Reference date.
 * @returns {?number} Whole days, or null.
 */
function daysUntil(iso, today) {
  const target = panoDateOrNull(iso);
  if (target == null) return null;
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  return Math.round((target - startOfToday) / 86400000);
}

/**
 * Flatten one city record from the artifact into the shape the sorter and the
 * row renderer both read.
 *
 * @param {Object} city - A `cities[]` entry from driving_plan.json.gz.
 * @param {Date} [today] - Reference date for the days-left column.
 * @returns {Object} Row model.
 */
function drivingRowModel(city, today = new Date()) {
  // Both blocks are absent-not-null in the artifact: an unlisted city carries
  // no `plan` key at all, and a never-collected one no `observed` key.
  const plan = city.plan ?? null;
  const observed = city.observed ?? {};
  const gsv = observed.gsv ?? null;
  const mly = observed.mapillary ?? null;

  // [firstYear, counts[]] or nothing. Validated once here so both the sort key
  // and the sparkline renderer can trust it.
  const raw = city.capture_years;
  const captureYears =
    Array.isArray(raw) && Number.isFinite(raw[0]) && Array.isArray(raw[1]) && raw[1].length
      ? raw
      : null;

  const planStatus = plan
    ? planStatusFor(
        // active_count is already computed with plan_match.is_published, so
        // this synthesizes the flag rather than re-reading a raw one.
        plan.active_count > 0 ? "Yes" : "No",
        plan.window_start ?? null,
        plan.window_end ?? null,
        today
      )
    : null;

  return {
    cityId: city.city_id ?? "",
    label: city.display_name || city.city_id || "Unknown",
    country: city.country_name ?? null,
    region: city.state_name ?? null,
    scope: "city",
    enabled: city.enabled === true,
    verdict: city.verdict ?? "not_listed",
    captureYears,
    // Sorts the sparkline column: the number of years between a place's first
    // and last observed capture. A wide span means repeat drives.
    //
    // Shape-guarded like sparklineCellHtml below, and for a harder reason: a
    // malformed `capture_years` here throws inside buildPlaceRows' map, which
    // aborts renderDrivingPlan before the table exists and leaves the whole
    // page showing "Error loading the driving plan" over one bad cell's worth
    // of data.
    captureSpanYears: captureYears ? captureYears[1].length - 1 : null,

    planStatus,
    windowStart: plan?.window_start ?? null,
    windowEnd: plan?.window_end ?? null,
    windowApproximate: plan?.window_approximate === true,
    daysToWindowEnd: daysUntil(plan?.window_end ?? null, today),
    matchTier: plan?.match_tier ?? null,
    // Joined into one string so the search box can find a city by the district
    // Google actually listed ("Ada" finds Boise) without a column for it.
    // Bounded by _MAX_CITY_DISTRICTS in json_summarizer: the artifact ships the
    // first few districts per city, sorted, so an early-alphabet county is
    // findable and a late one is not. Area rows carry their record's full list.
    districts: (plan?.districts ?? []).join(" "),

    coveragePct: gsv?.coverage_rate_pct ?? null,
    // Filled in by mergeStreetCoverage once the streetwalk manifest lands —
    // it is a separate artifact, and the page must render without it.
    streetPct: null,
    districtCount: city.plan?.districts_total ?? city.plan?.districts?.length ?? null,
    googlePanos: gsv?.google_panos ?? null,
    newestCapture: gsv?.newest_capture ?? null,
    yearsSinceCapture: gsv?.years_since_newest_capture ?? null,
    medianAge: gsv?.median_pano_age_years ?? null,
    captureDateChanged: gsv?.change?.capture_date_changed ?? null,
    lastRun: gsv?.run_date ?? mly?.run_date ?? null,

    mapillaryPct: mly?.coverage_rate_pct ?? null,

    // Prefer the GSV run for the city-page link: this page is about Google's
    // driving, so the GSV snapshot is the one a reader wants to open.
    filename: gsv?.csv_filename ?? mly?.csv_filename ?? null,
  };
}

/**
 * Flatten one plan record into the same row shape a city produces.
 *
 * These are the places Google names that we collect nothing for — the rows
 * that make the "Tracked?" column mean something. Every observed field is
 * null by construction, which the chassis already sinks in both sort
 * directions rather than treating as zero.
 *
 * @param {Object} record - A `records[]` entry from driving_plan.json.gz.
 * @param {Date} [today] - Reference date for the days-left column.
 * @returns {Object} Row model, shape-identical to drivingRowModel's.
 */
function planAreaRowModel(record, today = new Date()) {
  const region = record.region || null;
  const country = record.country_matched || record.country || null;
  const districts = record.districts ?? [];
  const districtCount = record.district_count ?? districts.length;

  // Name the row by what it actually covers, not just its region. Google's
  // feed carries TEN separate Accra records — different districts, different
  // windows — and labelling them all "Accra, Ghana" made ten distinct
  // campaigns look like one duplicated row. Where the feed is district-keyed
  // (most of the world outside the US) the district IS the place; where it is
  // region-keyed with the districts as mere enumeration (the US), the leading
  // district plus a count still distinguishes a state's concurrent campaigns.
  const place =
    districts.length && districts[0] !== region
      ? districts.length > 1
        ? `${districts[0]} +${districtCount - 1}`
        : districts[0]
      : region;
  const label =
    [place, place === region ? null : region, country].filter(Boolean).join(", ") || "Unknown";

  return {
    cityId: record.record_id ?? `plan:${region}`,
    label,
    country,
    region,
    scope: "area",
    enabled: false,
    verdict: record.verdict ?? "not_listed",
    captureYears: null,
    captureSpanYears: null,

    planStatus: planStatusFor(
      record.publish ?? null,
      record.window_start ?? null,
      record.window_end ?? null,
      today
    ),
    windowStart: record.window_start ?? null,
    windowEnd: record.window_end ?? null,
    windowApproximate: record.window_approximate === true,
    daysToWindowEnd: daysUntil(record.window_end ?? null, today),
    matchTier: null,
    districts: districts.join(" "),
    districtCount: districtCount || null,

    coveragePct: null,
    streetPct: null,
    googlePanos: null,
    newestCapture: null,
    yearsSinceCapture: null,
    medianAge: null,
    captureDateChanged: null,
    lastRun: null,
    mapillaryPct: null,
    filename: null,
  };
}

/**
 * Every place, from both halves of the artifact.
 *
 * A record that already matches tracked cities is deliberately skipped: those
 * cities are its rows, and emitting it too would double-count the same place —
 * Idaho would appear once as a plan area and again as Boise.
 *
 * @param {?Object} payload - Parsed driving_plan.json.gz.
 * @param {Date} [today] - Reference date.
 * @returns {Object[]} Row models.
 */
function buildPlaceRows(payload, today = new Date()) {
  const rows = (payload?.cities ?? []).map((city) => drivingRowModel(city, today));
  for (const record of payload?.records ?? []) {
    if ((record.matched_city_count ?? 0) > 0) continue;
    rows.push(planAreaRowModel(record, today));
  }
  return rows;
}

/**
 * Join road-walk street coverage onto the rows that have it.
 *
 * The walk lives in its own artifact (`streetwalks.json.gz`, issue #155)
 * because its spacing and run-date are independent of the grid run, so it
 * cannot be derived — the same reason the city page fetches it separately.
 *
 * Only the `drive` network is joined: `all_public` walks a much larger street
 * set (alleys, footpaths, park trails), so its percentage has a different
 * denominator and mixing the two into one column would silently change what
 * the number means between rows.
 *
 * @param {Object[]} rows - Row models; mutated in place.
 * @param {?Object} manifest - Parsed streetwalks.json.gz, or null.
 * @returns {number} How many rows gained a street-coverage figure.
 */
function mergeStreetCoverage(rows, manifest) {
  if (!manifest) return 0;
  let matched = 0;
  for (const row of rows) {
    if (row.scope !== "city") continue;
    const walk = lookupStreetwalk(manifest, row.cityId, "gsv", "drive");
    if (walk && walk.coverage_pct_by_length != null) {
      row.streetPct = walk.coverage_pct_by_length;
      matched += 1;
    }
  }
  return matched;
}

/**
 * Build one table row from a row model.
 *
 * @param {Object} row - From drivingRowModel or planAreaRowModel.
 * @param {Object[]} [columns] - Visible columns; defaults to all of them.
 * @returns {string} HTML for one <tr>.
 */
function drivingRowHtml(row, columns = DRIVING_COLUMNS) {
  return rowHtmlFromColumns(columns, row);
}

/**
 * One revision's changes, as a short human sentence plus its examples.
 *
 * Counters are exact; the example lists are capped by the artifact, so the
 * phrasing must never imply the examples are exhaustive.
 *
 * @param {Object} revision - A `revisions[]` entry.
 * @returns {string} HTML for one <li>.
 */
function revisionItemHtml(revision) {
  const parts = [];
  const push = (n, singular, plural) => {
    if (n > 0) parts.push(`${formatCellNumber(n)} ${n === 1 ? singular : plural}`);
  };
  push(revision.campaigns_closed, "campaign closed", "campaigns closed");
  push(revision.campaigns_reopened, "campaign reopened", "campaigns reopened");
  push(revision.windows_changed, "window moved", "windows moved");
  push(revision.districts_changed, "district list edited", "district lists edited");
  push(revision.regions_added, "region added", "regions added");
  push(revision.regions_removed, "region removed", "regions removed");

  const headline = parts.length ? parts.join(" · ") : "no regional changes";

  // Name a few of the places involved. Districts first: a rewritten district
  // list is where feed corruption shows up (Google once replaced Austria's 20
  // Steiermark districts with the single string "ibraltar").
  const detail = revision.detail ?? {};
  const examples = [
    ...(detail.districts ?? []).map(
      (d) =>
        `${d.region}${d.gained_count ? ` +${d.gained_count}` : ""}${d.lost_count ? ` −${d.lost_count}` : ""} districts`
    ),
    ...(detail.closed ?? []).map((d) => `${d.region} closed`),
    ...(detail.windows ?? []).map((d) => `${d.region} rescheduled`),
  ].slice(0, 6);

  const examplesHtml = examples.length
    ? `<span class="revision-examples">${escapeHtml(examples.join("; "))}</span>`
    : "";

  return (
    `<li><span class="revision-date">${escapeHtml(revision.from)} → ${escapeHtml(revision.to)}</span>` +
    `<span class="revision-headline">${escapeHtml(headline)}</span>${examplesHtml}</li>`
  );
}

/**
 * Render the plan-revision log.
 *
 * This is the archive's own reason to exist: Google overwrites the feed in
 * place, so without dated snapshots none of these edits would be observable at
 * all. The log is deliberately explicit about how shallow it still is — the
 * first snapshot is 2026-07-31 — because a short log must not read as "Google
 * rarely changes anything".
 *
 * @param {Object[]} revisions - The `revisions[]` collection.
 * @param {?Object} plan - The artifact's `plan` block, for archive depth.
 */
function renderRevisions(revisions, plan) {
  const el = document.getElementById("driving-revisions");
  if (!el) return;
  if (!revisions || revisions.length === 0) {
    el.innerHTML =
      `<h2>Plan revisions</h2><p class="streets-caveat">No revision has been captured yet. ` +
      `Google's feed is archived nightly${plan?.first_fetch ? ` since ${escapeHtml(plan.first_fetch)}` : ""}; ` +
      `a revision appears here the first time the published content changes.</p>`;
    el.hidden = false;
    return;
  }
  const items = revisions.map(revisionItemHtml).join("");
  el.innerHTML =
    `<h2>Plan revisions</h2>` +
    `<p class="streets-caveat">Google overwrites this feed in place, so these edits exist only ` +
    `because they were captured at the time. ${formatCellNumber(revisions.length)} revision` +
    `${revisions.length === 1 ? "" : "s"} recorded` +
    `${plan?.first_fetch ? ` since ${escapeHtml(plan.first_fetch)}` : ""} — a young archive, not ` +
    `a complete history of Google's planning.</p>` +
    `<ul class="revision-list">${items}</ul>`;
  el.hidden = false;
}

// The table + controls controllers (created on first render so a header click
// or a filter change can repaint without refetching).
let drivingTable = null;
let drivingControls = null;

/**
 * Render the table (or the empty state) from the artifact payload.
 *
 * @param {?Object} payload - Parsed driving_plan.json.gz, or null.
 */
function renderDrivingPlan(payload, manifest = null) {
  const statusEl = document.getElementById("driving-status");
  const wrapEl = document.getElementById("driving-table-wrap");

  const today = new Date();
  const rows = buildPlaceRows(payload, today);
  mergeStreetCoverage(rows, manifest);

  if (rows.length === 0) {
    statusEl.textContent = "No driving-plan data has been published yet.";
    return;
  }

  renderPlanProvenance(payload?.plan ?? null);
  renderRevisions(payload?.revisions ?? [], payload?.plan ?? null);

  drivingTable ??= createSortableTable({
    columns: DRIVING_COLUMNS,
    defaultSort: DRIVING_DEFAULT_SORT,
    theadEl: document.getElementById("driving-thead"),
    tbodyEl: document.getElementById("driving-tbody"),
  });
  drivingControls ??= createTableControls({
    rootEl: document.getElementById("driving-controls"),
    table: drivingTable,
    columns: DRIVING_COLUMNS,
    presets: DRIVING_PRESETS,
    filters: DRIVING_FILTERS,
    searchFields: DRIVING_SEARCH_FIELDS,
    // Rows here are places, not cities, and the districts Google lists are
    // searchable too — "Ada" finds Boise — so the default "City, provider…"
    // would understate what the box does.
    searchPlaceholder: "City, region, county…",
    onChange: (shown, all) => updateDrivingCaption(shown, all, payload?.generated_at ?? null),
  });
  drivingControls.setRows(rows);

  statusEl.hidden = true;
  wrapEl.hidden = false;
}

/**
 * State how thin the archive still is.
 *
 * This is the page's main caveat, not a footnote: the first snapshot is
 * 2026-07-31, so for any drive that already happened the join can only say
 * "the plan is silent or stale". Without this line a reader would take an
 * empty plan cell as a statement about Google's intentions in, say, 2023,
 * when in fact we simply were not watching yet.
 *
 * @param {?Object} plan - The artifact's `plan` block.
 */
function renderPlanProvenance(plan) {
  const el = document.getElementById("driving-provenance");
  if (!el || !plan) return;
  const parts = [];
  if (plan.first_fetch) {
    parts.push(
      `Archived from Google's feed since <strong>${escapeHtml(plan.first_fetch)}</strong>`
    );
  }
  if (plan.fetch_count) {
    parts.push(
      `${formatCellNumber(plan.fetch_count)} fetches, ` +
        `${formatCellNumber(plan.change_count)} of which saw the feed change`
    );
  }
  if (plan.latest_change) {
    parts.push(`last revision <strong>${escapeHtml(plan.latest_change)}</strong>`);
  }
  el.innerHTML =
    `<p>${parts.join(" · ")}.</p>` +
    (plan.disclaimer ? `<p class="streets-caveat">${escapeHtml(plan.disclaimer)}</p>` : "");
  el.hidden = false;
}

/**
 * Keep the caption reporting what is actually on screen.
 *
 * @param {Object[]} shown - Filtered rows.
 * @param {Object[]} all - Every row.
 * @param {?string} generatedAt - Artifact timestamp.
 */
function updateDrivingCaption(shown, all, generatedAt) {
  const tracked = shown.filter((r) => r.scope === "city").length;
  const counts =
    shown.length === all.length
      ? `${all.length} places · ${tracked} tracked`
      : `${shown.length} of ${all.length} places · ${tracked} tracked`;
  document.getElementById("driving-caption").textContent =
    counts + (generatedAt ? ` · updated ${new Date(generatedAt).toLocaleString()}` : "");
}

/**
 * Fetch the artifacts, then render.
 *
 * Asymmetric criticality, the same split streets.js uses: the driving-plan
 * join IS the page and its failure is fatal, while the streetwalk manifest
 * only adds one optional column — a deployment that has never run a road walk
 * publishes no manifest at all, so its absence degrades the table rather than
 * failing it.
 */
async function loadDrivingPlan() {
  try {
    const [payload, manifest] = await Promise.all([
      fetchGzippedJson(STREETSCAPE_DATA_BASE_URL + "driving_plan.json.gz"),
      fetchStreetwalkManifest(),
    ]);
    renderDrivingPlan(payload, manifest);
  } catch (error) {
    console.error("Error loading driving plan:", error);
    document.getElementById("driving-status").textContent =
      "Error loading the driving plan. Please check the console for details.";
  }
}

// Guarded so `require`ing this file in the Node unit tests (which have no
// document) exercises the pure helpers without trying to load anything.
if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", loadDrivingPlan);
}

// Node/CommonJS export shim for the unit tests. No-op in the browser, where
// these are plain globals loaded via <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    drivingRowModel,
    planAreaRowModel,
    buildPlaceRows,
    mergeStreetCoverage,
    planStatusFor,
    windowRangeCellHtml,
    drivingRowHtml,
    daysUntil,
    sparklineCellHtml,
    revisionItemHtml,
    renderDrivingPlan,
    updateDrivingCaption,
    VERDICTS,
    DRIVING_COLUMNS,
    DRIVING_PRESETS,
    DRIVING_FILTERS,
    DRIVING_SEARCH_FIELDS,
    DRIVING_DEFAULT_SORT,
  };
}
