"""
Match tracked cities against Google's published driving plan (issue #176).

Pure logic — no I/O, no database, no network — so every rule below is unit
testable and the artifact generator stays a thin assembly layer.

**Why this is a string match and not geometry.** The feed keys US entries by
county, which looks like it demands city→county boundary resolution. Measured
against the production catalog on 2026-08-16 it does not: there are 17 distinct
windows across 1,981 US entries in 51 states, and 49 of those 51 states give
every listed county the *same* window (the exceptions, Idaho and Oregon, carry
one active 2026 window plus one closed 2025-12 one). Google publishes a seasonal
window per state and then enumerates the counties it covers, so county
granularity carries no additional signal. A plain match of ``cities.state_name``
against the feed's ``region`` resolves 1,112 of 1,113 US cities — 92% of the
catalog — for the cost of a dict lookup. The one thing county resolution would
add is whether a *specific* county sits in the listed set, and Google's own note
says listed areas "may include smaller cities and towns within driving
distance", so an unlisted county is not evidence of anything.

**A match is a SET of entries, never one row.** Picking a single representative
entry would be arbitrary: a US region match selects every county of the state
(which is fine — they share a window), while an Israeli country-level match
selects 21 districts whose windows differ. `summarize_entries` reduces whichever
set matched to the span the page renders, so the ambiguity is collapsed once, in
the open, instead of silently at selection time.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

# ── Verdict vocabulary ─────────────────────────────────────────────────────
#
# One string per row, with every metric behind it published alongside so a
# reader can re-derive it — the shape boundary_audit.AuditRow already uses
# ("verdict plus every metric behind it").
VERDICT_NOT_LISTED = "not_listed"
VERDICT_DRIVE_CONFIRMED = "drive_confirmed"
VERDICT_PLANNED_OPEN = "planned_open"
VERDICT_PLANNED_UPCOMING = "planned_upcoming"
VERDICT_DRIVEN_UNPLANNED = "driven_unplanned"
VERDICT_CLOSED = "closed"

# How far past a campaign's last published window an observed capture has to
# fall before we call it a drive the feed never announced. Six months absorbs
# the ordinary case of a window that slipped or was extended without a feed
# revision; Israel's gap (2019-03 window end vs 2023-10 imagery) is 4.5 years,
# so this threshold is nowhere near the interesting cases.
UNPLANNED_DRIVE_SLACK = timedelta(days=183)

# ── Country normalization ──────────────────────────────────────────────────
#
# The feed writes some country names in the local language and misspells
# others, so a naive join silently reports zero plan entries for countries we
# actively track. Every alias below was confirmed present in the production
# catalog — without them Spain, Mexico and Brazil all read as "not in plan"
# while their entries sit there under another spelling.
_COUNTRY_ALIASES = {
    "brasil": "Brazil",
    "espana": "Spain",  # España, after diacritic folding
    "italia": "Italy",
    "eesti": "Estonia",
    "hrvatska": "Croatia",
    "kazahkstan": "Kazakhstan",  # the feed's own misspelling
    "fyrom": "North Macedonia",
    # The feed carries BOTH of these as separate countries; fold them together
    # so a city in either resolves to one plan picture.
    "bosnia": "Bosnia and Herzegovina",
    "bosnia and herzegovina": "Bosnia and Herzegovina",
    "usa": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "great britain": "United Kingdom",
    "cote d'ivoire": "Ivory Coast",
    "czechia": "Czech Republic",
}

# Administrative suffixes and prefixes that differ between Nominatim's naming
# (what `cities.state_name` holds) and the feed's. Confirmed needed by rows
# already in the catalog: "O'Higgins Region", "Cauca Department", "State of
# Vienna", "Alajuela Province", "Haifa District".
_ADMIN_SUFFIXES = (
    "county",
    "parish",
    "borough",
    "region",
    "department",
    "departamento",
    "province",
    "provincia",
    "district",
    "prefecture",
    "municipality",
    "metropolitan region",
    "state",
)
_ADMIN_PREFIXES = ("state of", "province of", "region of", "department of")

# Cities normalization genuinely cannot reach. Keyed by city_id → the feed
# `region` or `district` string to match on, with the country it lives in.
# Deliberately tiny: every entry here is a place where the feed and Nominatim
# disagree in a way no general rule fixes.
MANUAL_LINKS: dict[str, tuple[str, str]] = {
    # Israel's feed rows are written in Hebrew while our city_name is Latin
    # script, so neither the region nor the district tier can ever fire. These
    # two matter because they are the cities the page was built to answer for.
    "tel-aviv--tel-aviv-district--israel": ("Israel", "תל אביב"),
    "haifa--haifa-district--israel": ("Israel", "חיפה"),
    # Nominatim calls it "District of Columbia"; the feed calls it
    # "Washington DC". Note the feed ALSO has a region literally named
    # "Washington" — that one is the state, and Washington-state cities match
    # it correctly on their own, so this override must name the DC row exactly
    # rather than relying on any prefix rule.
    "washington--district-of-columbia--united-states": ("United States", "Washington DC"),
}


def _fold(value: str) -> str:
    """
    Casefold and strip diacritics. Mirrors `foldForSearch` in
    www/js/table-controls.js so the Python join and the browser's search box
    agree on what "the same string" means.
    """
    decomposed = unicodedata.normalize("NFD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


def normalize_country(name: str | None) -> str:
    """
    Canonical country name for joining, or "" for a missing value.

    >>> normalize_country("Brasil")
    'Brazil'
    >>> normalize_country("España")
    'Spain'
    >>> normalize_country("United States")
    'United States'
    """
    if not name:
        return ""
    folded = _fold(name)
    return _COUNTRY_ALIASES.get(folded, name.strip())


def normalize_admin(name: str | None) -> str:
    """
    Fold an administrative name (state / region / county / district) to a
    comparable key: diacritics stripped, casefolded, and the administrative
    suffix or prefix removed.

    >>> normalize_admin("O'Higgins Region") == normalize_admin("O'Higgins")
    True
    >>> normalize_admin("State of Vienna") == normalize_admin("Vienna")
    True
    >>> normalize_admin("King County") == normalize_admin("King")
    True
    """
    if not name:
        return ""
    folded = _fold(name)
    for prefix in _ADMIN_PREFIXES:
        if folded.startswith(prefix + " "):
            folded = folded[len(prefix) + 1 :]
            break
    # Longest suffix first so "metropolitan region" wins over "region".
    for suffix in sorted(_ADMIN_SUFFIXES, key=len, reverse=True):
        if folded.endswith(" " + suffix):
            folded = folded[: -(len(suffix) + 1)]
            break
    return folded.strip()


@dataclass(frozen=True)
class PlanIndex:
    """
    Plan entries bucketed for lookup. Built once per artifact generation and
    reused for every city — the naive alternative rescans ~11.7k entries per
    city, which is 14M comparisons over the catalog.
    """

    by_country: Mapping[str, Sequence[Any]]
    by_region: Mapping[tuple[str, str], Sequence[Any]]
    by_district: Mapping[tuple[str, str], Sequence[Any]]


def build_index(entries: Iterable[Any]) -> PlanIndex:
    """Bucket plan entries by normalized country, (country, region) and (country, district)."""
    by_country: dict[str, list[Any]] = {}
    by_region: dict[tuple[str, str], list[Any]] = {}
    by_district: dict[tuple[str, str], list[Any]] = {}
    for entry in entries:
        country = normalize_country(entry["country"])
        by_country.setdefault(country, []).append(entry)
        region = normalize_admin(entry["region"])
        if region:
            by_region.setdefault((country, region), []).append(entry)
        district = normalize_admin(entry["district"])
        if district:
            by_district.setdefault((country, district), []).append(entry)
    return PlanIndex(by_country=by_country, by_region=by_region, by_district=by_district)


def match_city(city: Any, index: PlanIndex) -> tuple[str | None, list[Any]]:
    """
    Find the plan entries covering one city.

    Returns ``(tier, entries)`` where tier is one of ``"manual"``, ``"region"``,
    ``"district"``, ``"country"`` or ``None`` (no entries at all for the
    country). The tier is published so the page can show match confidence
    rather than implying every row was resolved equally well.

    Region is tried before district on purpose: the *window* is a property of
    the region (one per state, per the module docstring), so a region match
    lands on the level that actually carries the information. A district match
    is the fallback for feeds like Israel's that are keyed by city name — and
    it is genuinely ambiguous in the US, where Idaho has both an Ada county
    (containing Boise city) and a Boise county. That ambiguity is harmless
    precisely because both counties share Idaho's single window, but it is the
    reason district does not outrank region.
    """
    country = normalize_country(getattr(city, "country_name", None))

    manual = MANUAL_LINKS.get(city.city_id)
    if manual is not None:
        manual_country, manual_name = manual
        key = (normalize_country(manual_country), normalize_admin(manual_name))
        hits = list(index.by_region.get(key, ())) or list(index.by_district.get(key, ()))
        if hits:
            return "manual", hits
        # A stale override (the feed renamed or dropped the row) must not strand
        # the city — fall through to the ordinary tiers rather than reporting
        # "not listed" for a country that is plainly in the plan.

    region_key = (country, normalize_admin(getattr(city, "state_name", None)))
    if region_key[1]:
        hits = index.by_region.get(region_key)
        if hits:
            return "region", list(hits)

    district_key = (country, normalize_admin(getattr(city, "city_name", None)))
    if district_key[1]:
        hits = index.by_district.get(district_key)
        if hits:
            return "district", list(hits)

    hits = index.by_country.get(country)
    if hits:
        return "country", list(hits)

    return None, []


_LOOSE_DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$")


def parse_loose_date(value: str | None) -> date | None:
    """
    Day-first ``D/M/YY`` fallback for the feed's dirty dates.

    ``driving_plan.parse_feed_date`` is deliberately strict ISO and stores NULL
    beside the raw string, so no record is dropped and nothing guesses inside
    the catalog. That is the right call for storage and the wrong one for a
    verdict: Israel's rows are ALL dirty (``14/2/19``, ``28/11/18``), so
    without a fallback the one country this page was built to answer for has no
    computable window at all and silently reads as "closed" instead of the
    contradiction it actually is.

    Day-first is not a guess. Unambiguous values in the feed — ``28/11/18``,
    ``25/11/18``, ``16/12/18`` — all carry a first component above 12, so the
    format is D/M/YY throughout. Two-digit years are 2000-relative; every dirty
    value observed is 18 or 19.

    Used only for classification and clearly marked ``approximate`` where it is
    published, never written back to the catalog.

    >>> parse_loose_date("28/11/18")
    datetime.date(2018, 11, 28)
    >>> parse_loose_date("2019-08-01T07:00:00.000Z") is None
    True
    """
    if not value:
        return None
    m = _LOOSE_DATE_RE.match(value)
    if m is None:
        return None
    day, month, year = (int(g) for g in m.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def record_key(entry: Any) -> tuple:
    """
    The identity of the feed *record* an exploded entry came from.

    ``driving_plan.explode_records`` writes one catalog row per (record,
    district) because the feed comma-joins districts, so everything except
    ``district`` is shared by a record's rows. Regrouping on this key inverts
    that explosion — 11,765 entries back into 3,715 records — and lets two
    passes over the entries agree on which record they are talking about
    without relying on object identity.
    """
    return (
        entry["country"],
        entry["code"],
        entry["svspc"],
        entry["region"],
        entry["publish"],
        entry["date_start_raw"],
        entry["date_end_raw"],
    )


def region_key(entry: Any) -> tuple[str, str]:
    """
    The (country, region) a record belongs to, normalized.

    Coarser than `record_key`: a window shifting or a `publish` flip changes
    the record key but not the region, which is what lets a revision diff say
    "Idaho's window moved" instead of "one record vanished and another
    appeared". Regions are the level Google actually schedules at.
    """
    return (normalize_country(entry["country"]), normalize_admin(entry["region"]))


def diff_snapshots(before: Sequence[Any], after: Sequence[Any]) -> dict[str, Any]:
    """
    What changed between two consecutive *content-changed* plan snapshots.

    Entries exist only for changed snapshots, so consecutive members of
    ``db.get_changed_driving_plan_snapshots`` are exactly the comparable pairs
    — there is no gap to reason about and no need to reconstruct an unchanged
    fetch's contents.

    Grouped by (country, region) rather than by record, because the feed's
    interesting edits are *mutations* of a region's plan: a window sliding, a
    campaign closing (`publish` Yes→No), a district list being rewritten. Keyed
    by record instead, every one of those reads as an unrelated delete plus
    insert, which is true but useless.

    The district comparison is what surfaces feed corruption: on 2026-08-11
    Google replaced Austria/Steiermark's twenty districts with the single
    string ``ibraltar`` — "Gibraltar" minus its first character, lifted from an
    unrelated record — and left it live. That is reported here as a district
    change on one region, which is exactly what it is.

    Returns counters plus per-region detail, both bounded: a revision that
    rewrote half the feed should not publish half the feed again.
    """
    def _by_region(entries: Sequence[Any]) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in entries:
            key = region_key(entry)
            slot = out.setdefault(
                key,
                {
                    "country": entry["country"],
                    "region": entry["region"],
                    "districts": set(),
                    "publish": set(),
                    "windows": set(),
                },
            )
            if entry["district"]:
                slot["districts"].add(entry["district"])
            slot["publish"].add((entry["publish"] or "").strip())
            start, _ = entry_date(entry, "date_start", "date_start_raw")
            end, _ = entry_date(entry, "date_end", "date_end_raw")
            slot["windows"].add((start, end))
        return out

    old, new = _by_region(before), _by_region(after)

    added = [new[k] for k in new.keys() - old.keys()]
    removed = [old[k] for k in old.keys() - new.keys()]

    closed: list[dict[str, Any]] = []
    reopened: list[dict[str, Any]] = []
    window_changed: list[dict[str, Any]] = []
    districts_changed: list[dict[str, Any]] = []

    for key in old.keys() & new.keys():
        a, b = old[key], new[key]
        entry = {"country": b["country"], "region": b["region"]}

        # The campaign-closed signal: something was published and no longer is.
        was_live, is_live = "Yes" in a["publish"], "Yes" in b["publish"]
        if was_live and not is_live:
            closed.append(entry)
        elif is_live and not was_live:
            reopened.append(entry)

        if a["windows"] != b["windows"]:
            window_changed.append(
                {**entry, "from": sorted(filter(None, (w for p in a["windows"] for w in p)))[:2],
                 "to": sorted(filter(None, (w for p in b["windows"] for w in p)))[:2]}
            )

        if a["districts"] != b["districts"]:
            gained = sorted(b["districts"] - a["districts"])
            lost = sorted(a["districts"] - b["districts"])
            districts_changed.append(
                {
                    **entry,
                    "gained": gained[:_MAX_DETAIL],
                    "lost": lost[:_MAX_DETAIL],
                    "gained_count": len(gained),
                    "lost_count": len(lost),
                }
            )

    return {
        "regions_added": len(added),
        "regions_removed": len(removed),
        "campaigns_closed": len(closed),
        "campaigns_reopened": len(reopened),
        "windows_changed": len(window_changed),
        "districts_changed": len(districts_changed),
        "detail": {
            "added": [{"country": r["country"], "region": r["region"]} for r in added][:_MAX_DETAIL],
            "removed": [
                {"country": r["country"], "region": r["region"]} for r in removed
            ][:_MAX_DETAIL],
            "closed": closed[:_MAX_DETAIL],
            "reopened": reopened[:_MAX_DETAIL],
            "windows": window_changed[:_MAX_DETAIL],
            "districts": districts_changed[:_MAX_DETAIL],
        },
    }


# How many per-region examples a revision publishes per category. The counters
# above are always exact; only the illustrative lists are capped, so a feed-wide
# edit cannot republish the feed.
_MAX_DETAIL = 25


def entry_date(entry: Any, parsed_key: str, raw_key: str) -> tuple[str | None, bool]:
    """
    One entry's date as ISO, preferring the catalog's strict parse and falling
    back to the day-first reading of the raw string. Returns (iso, approximate).
    """
    parsed = entry[parsed_key]
    if parsed:
        return parsed[:10], False
    loose = parse_loose_date(entry[raw_key])
    if loose is not None:
        return loose.isoformat(), True
    return None, False


@dataclass(frozen=True)
class PlanSummary:
    """The span a matched entry set covers, reduced to what the page renders."""

    entry_count: int
    active_count: int
    window_start: str | None
    window_end: str | None
    districts: list[str]
    # True when any bound came from the day-first fallback above rather than
    # the feed's own ISO value, so the page can mark the window approximate
    # instead of presenting a heuristic as published fact.
    approximate: bool = False

    @property
    def is_active(self) -> bool:
        return self.active_count > 0


def summarize_entries(entries: Sequence[Any]) -> PlanSummary:
    """
    Reduce a matched entry set to one window.

    When any entry is ``publish='Yes'`` the window spans only the active ones —
    a closed 2019 campaign sitting beside a live 2026 one must not stretch the
    reported window back seven years. With nothing active, the span covers all
    entries, which is what makes "this campaign ended on <date>" answerable.
    """
    active = [e for e in entries if (e["publish"] or "").strip().casefold() == "yes"]
    considered = active or list(entries)

    starts: list[str] = []
    ends: list[str] = []
    approximate = False
    for entry in considered:
        iso, approx = entry_date(entry,"date_start", "date_start_raw")
        if iso:
            starts.append(iso)
            approximate = approximate or approx
        iso, approx = entry_date(entry,"date_end", "date_end_raw")
        if iso:
            ends.append(iso)
            approximate = approximate or approx

    districts = sorted({e["district"] for e in entries if e["district"]})
    return PlanSummary(
        entry_count=len(entries),
        active_count=len(active),
        window_start=min(starts) if starts else None,
        window_end=max(ends) if ends else None,
        districts=districts,
        approximate=approximate,
    )


def classify(
    summary: PlanSummary | None,
    newest_capture: date | None,
    today: date,
) -> str:
    """
    One verdict per city, from the plan span and the newest capture date we
    have observed.

    Ordering is by actionability, not by source: a confirmed drive outranks a
    plan status because it is the stronger fact, and ``driven_unplanned`` is
    checked only after the plan's own statuses so a live campaign is not
    mislabelled by imagery that predates it.

    ``driven_unplanned`` is the finding that motivated the page — Israel's feed
    rows closed in 2019 while its imagery is from 2023 — and it is the reason a
    ``closed`` or ``not_listed`` verdict must never be read as "not driven".
    """
    if summary is None or summary.entry_count == 0:
        return VERDICT_NOT_LISTED

    start = _parse_iso(summary.window_start)
    end = _parse_iso(summary.window_end)

    if newest_capture is not None and start is not None and end is not None:
        if start <= newest_capture <= end:
            return VERDICT_DRIVE_CONFIRMED

    if summary.is_active:
        if start is not None and today < start:
            return VERDICT_PLANNED_UPCOMING
        if end is None or today <= end:
            return VERDICT_PLANNED_OPEN

    if newest_capture is not None and end is not None:
        if newest_capture > end + UNPLANNED_DRIVE_SLACK:
            return VERDICT_DRIVEN_UNPLANNED

    return VERDICT_CLOSED


def _parse_iso(value: str | None) -> date | None:
    """Parse a 'YYYY-MM-DD' catalog date, tolerating None and dirty values."""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


# Street View launched in May 2007, so no capture predates it. Anything earlier
# — or dated in the future — is a third-party photosphere's garbage EXIF, not an
# observation.
EARLIEST_PLAUSIBLE_CAPTURE = date(2007, 1, 1)


def plausible_capture_date(value: str | None, today: date) -> date | None:
    """
    A run's capture date, or None when the catalog's value cannot be true.

    ``runs.oldest_capture_date`` / ``newest_capture_date`` are computed over
    EVERY pano in a run, not just the official ``© Google`` ones, so a single
    user-contributed photosphere with corrupt EXIF poisons the column for the
    whole city. On the production catalog as of 2026-08-16 that is 21 runs
    dated in the future (Ho Chi Minh City and Covington read ``2612-01-01``;
    Chicago, San Francisco, Toronto, Cape Town and São Paulo read
    ``2611-09-01``) and 75 dated before Street View existed (``1970-08-01``,
    ``1980-01-01``).

    Publishing those would put an absurd "newest capture" on the page and, far
    worse, manufacture a ``driven_unplanned`` verdict out of nothing — the
    claim this page exists to make, invented by a typo. So an implausible value
    is treated as absent, consistent with the absent-not-null convention: we
    would rather say "no data" than publish a number known to be wrong.

    Fixing the underlying columns is a separate concern from rendering them.
    """
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    if parsed < EARLIEST_PLAUSIBLE_CAPTURE or parsed > today:
        return None
    return parsed
