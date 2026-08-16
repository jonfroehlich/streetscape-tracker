"""
Tests for `plan_match` — matching tracked cities against Google's published
driving plan, and turning the pair into a verdict (issue #176).

Pure logic, so these need no catalog and no fixtures. Every constant asserted
here was confirmed present in the production catalog on 2026-08-16; the point
of pinning them is that a silently-broken alias reports "not in the plan" for a
country that is plainly in it, which reads as a finding rather than a bug.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from streetscape_metadata_tracker import plan_match

TODAY = date(2026, 8, 16)


def _entry(**overrides):
    """One exploded plan entry. Dict access matches the sqlite3.Row interface."""
    row = {
        "country": "United States",
        "code": "US",
        "svspc": "SV",
        "region": "Idaho",
        "district": "Ada",
        "publish": "Yes",
        "date_start": "2026-04-13",
        "date_start_raw": "2026-04-13T07:00:00.000Z",
        "date_end": "2026-11-01",
        "date_end_raw": "2026-11-01T07:00:00.000Z",
    }
    row.update(overrides)
    return row


def _city(**overrides):
    fields = {
        "city_id": "boise--idaho--united-states",
        "city_name": "Boise",
        "state_name": "Idaho",
        "country_name": "United States",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# ── Normalization ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "feed_name,expected",
    [
        ("Brasil", "Brazil"),
        ("España", "Spain"),
        ("Italia", "Italy"),
        ("Eesti", "Estonia"),
        ("Hrvatska", "Croatia"),
        ("Kazahkstan", "Kazakhstan"),  # the feed's own misspelling
        ("FYROM", "North Macedonia"),
        ("Bosnia", "Bosnia and Herzegovina"),
        ("Bosnia and Herzegovina", "Bosnia and Herzegovina"),
        ("United States", "United States"),  # untouched
    ],
)
def test_country_aliases_fold_the_feeds_local_spellings(feed_name, expected):
    assert plan_match.normalize_country(feed_name) == expected


def test_normalize_country_tolerates_missing_values():
    assert plan_match.normalize_country(None) == ""
    assert plan_match.normalize_country("") == ""


def test_normalize_country_preserves_the_spelling_it_publishes():
    # It is a DISPLAY name — a record's published `country_matched` — so
    # anything the alias table does not cover keeps its case and diacritics.
    # Folding here would put "mexico" on screen; `country_key` is the join.
    assert plan_match.normalize_country("México") == "México"
    assert plan_match.normalize_country("  Japan  ") == "Japan"


@pytest.mark.parametrize(
    "catalog_spelling,feed_spelling",
    [
        ("México", "Mexico"),  # the case the alias table never covered
        ("Mexico", "MEXICO"),
        ("Brasil", "BRAZIL"),  # alias AND case, together
        ("Panamá", "panama"),
        ("Türkiye", "TURKIYE"),
    ],
)
def test_country_key_joins_spellings_that_differ_only_by_case_or_accent(
    catalog_spelling, feed_spelling
):
    # `normalize_country` folds a name only to LOOK UP the alias table and then
    # returns the raw spelling, so every country outside that 15-entry table
    # stayed case- and diacritic-sensitive. A catalog "México" bucketed under a
    # different key than a feed "Mexico" resolved every city there to
    # `not_listed` while its records simultaneously showed up as untracked plan
    # areas — the same country on the page twice, disagreeing with itself.
    assert plan_match.country_key(catalog_spelling) == plan_match.country_key(feed_spelling)


def test_country_key_still_separates_genuinely_different_countries():
    # Folding must not over-merge: the guard against "fix the join by making
    # every key collide".
    assert plan_match.country_key("Austria") != plan_match.country_key("Australia")
    assert plan_match.country_key(None) == ""


def test_a_city_matches_a_feed_country_spelled_with_an_accent():
    # End-to-end through the index, which is where the bug actually bit: both
    # `build_index` and `match_city` have to agree on the key.
    index = plan_match.build_index([_entry(country="México", region="Jalisco")])
    tier, hits = plan_match.match_city(
        _city(city_id="guadalajara--jalisco--mexico", state_name="Jalisco", country_name="Mexico"),
        index,
    )
    assert tier == "region"
    assert len(hits) == 1


def test_region_key_groups_two_spellings_of_one_country_together():
    # A grouping key, so a re-spelled row must collapse rather than read as a
    # region removed plus an unrelated region added.
    assert plan_match.region_key(
        _entry(country="México", region="Jalisco")
    ) == plan_match.region_key(_entry(country="Mexico", region="Jalisco"))


# ── The publish flag ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Yes", True),
        (" Yes ", True),  # the feed ships untrimmed values
        ("yes", True),
        ("YES", True),
        ("No", False),
        (" no ", False),
        ("", False),
        (None, False),
    ],
)
def test_is_published_is_one_reading_of_the_flag(raw, expected):
    # Three call sites used to test this three different ways: `.strip()
    # .casefold()` in summarize_entries, a bare `.strip()` in diff_snapshots,
    # and an exact `!== "Yes"` in driving.js. A " Yes " counted as live in one
    # place and closed in another, producing a row whose Verdict said "Driving
    # now" beside a Plan status of "Closed".
    assert plan_match.is_published(raw) is expected


def test_an_untrimmed_publish_flag_is_live_everywhere_it_is_read():
    # The two Python readers, on the same value, must agree.
    padded = _entry(publish=" Yes ")
    assert plan_match.summarize_entries([padded]).active_count == 1

    diff = plan_match.diff_snapshots([_entry(publish="No")], [padded])
    assert diff["campaigns_reopened"] == 1
    assert diff["campaigns_closed"] == 0


@pytest.mark.parametrize(
    "catalog_name,feed_name",
    [
        ("O'Higgins Region", "O'Higgins"),
        ("Cauca Department", "Cauca"),
        ("State of Vienna", "Vienna"),
        ("Alajuela Province", "Alajuela"),
        ("Haifa District", "Haifa"),
        ("King County", "King"),
        ("São Paulo", "Sao Paulo"),  # diacritics folded
    ],
)
def test_admin_suffixes_and_diacritics_fold_to_the_same_key(catalog_name, feed_name):
    assert plan_match.normalize_admin(catalog_name) == plan_match.normalize_admin(feed_name)


def test_normalize_admin_prefers_the_longest_matching_suffix():
    # "metropolitan region" must win over "region", or the leftover word
    # silently blocks the match.
    assert plan_match.normalize_admin("Santiago Metropolitan Region") == "santiago"


def test_normalize_admin_does_not_strip_a_name_that_merely_contains_a_suffix():
    # "District of Columbia" ends in "columbia", not "district" — stripping it
    # would be wrong, and DC is handled by an explicit manual link instead.
    assert plan_match.normalize_admin("District of Columbia") == "district of columbia"


# ── Dirty dates ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("28/11/18", date(2018, 11, 28)),
        ("13/1/19", date(2019, 1, 13)),
        ("16/12/18", date(2018, 12, 16)),
        ("14/2/19", date(2019, 2, 14)),
    ],
)
def test_dirty_feed_dates_parse_day_first(raw, expected):
    # Day-first is not a guess: values like 28/11/18 and 16/12/18 carry a first
    # component above 12, so the format is unambiguous across the feed.
    assert plan_match.parse_loose_date(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "Septemb", "2019-08-01T07:00:00.000Z", "99/99/99"])
def test_loose_parse_refuses_everything_it_cannot_read(raw):
    assert plan_match.parse_loose_date(raw) is None


def test_a_window_recovered_from_dirty_dates_is_flagged_approximate():
    # Israel's rows are ALL dirty, so without the fallback the country this
    # page was built for has no computable window at all — but a recovered
    # bound must never present as a published one.
    entries = [
        _entry(
            country="Israel",
            region="תל אביב",
            district="תל אביב",
            publish="No",
            date_start=None,
            date_start_raw="14/2/19",
            date_end=None,
            date_end_raw="28/2/19",
        )
    ]
    summary = plan_match.summarize_entries(entries)
    assert summary.window_start == "2019-02-14"
    assert summary.window_end == "2019-02-28"
    assert summary.approximate is True


def test_a_clean_window_is_not_flagged_approximate():
    assert plan_match.summarize_entries([_entry()]).approximate is False


# ── Matching ───────────────────────────────────────────────────────────────


def test_region_match_wins_because_the_window_is_a_property_of_the_region():
    index = plan_match.build_index([_entry(), _entry(district="Boise")])
    tier, matched = plan_match.match_city(_city(), index)
    assert tier == "region"
    # Both Idaho counties come back: they share one window, which is exactly
    # why county-level resolution buys nothing.
    assert len(matched) == 2


def test_district_match_covers_feeds_keyed_by_city_name():
    index = plan_match.build_index(
        [_entry(country="Jordan", region="Amman", district="Amman", publish="No")]
    )
    city = _city(
        city_id="amman--amman--jordan",
        city_name="Amman",
        state_name="Governorate X",  # deliberately not the feed's region
        country_name="Jordan",
    )
    tier, matched = plan_match.match_city(city, index)
    assert tier == "district"
    assert len(matched) == 1


def test_country_only_match_is_the_weakest_tier_and_is_reported_as_such():
    index = plan_match.build_index([_entry(country="Israel", region="חיפה", district="חיפה")])
    city = _city(
        city_id="somewhere--x--israel", city_name="Somewhere", state_name="X", country_name="Israel"
    )
    tier, matched = plan_match.match_city(city, index)
    assert tier == "country"
    assert len(matched) == 1


def test_an_unlisted_country_matches_nothing():
    index = plan_match.build_index([_entry()])
    city = _city(city_id="addis", city_name="Addis Ababa", state_name="A", country_name="Ethiopia")
    tier, matched = plan_match.match_city(city, index)
    assert tier is None
    assert matched == []


def test_manual_links_reach_what_normalization_cannot():
    # Israel's feed rows are Hebrew against our Latin-script names, so no
    # general rule can ever fire.
    index = plan_match.build_index(
        [_entry(country="Israel", region="תל אביב", district="תל אביב", publish="No")]
    )
    city = _city(
        city_id="tel-aviv--tel-aviv-district--israel",
        city_name="Tel-Aviv",
        state_name="Tel-Aviv District",
        country_name="Israel",
    )
    tier, matched = plan_match.match_city(city, index)
    assert tier == "manual"
    assert len(matched) == 1


def test_every_manual_link_names_a_city_id_shaped_key():
    # A typo'd key is silently inert, so pin the shape. These are real
    # production city_ids.
    for city_id, (country, name) in plan_match.MANUAL_LINKS.items():
        assert city_id == city_id.lower()
        assert "--" in city_id, f"{city_id} is not a derived city_id"
        assert country and name


def test_a_stale_manual_link_falls_through_instead_of_stranding_the_city():
    # If the feed renames or drops the row an override points at, the city must
    # still resolve by the ordinary tiers rather than reporting "not listed"
    # for a country plainly in the plan.
    index = plan_match.build_index([_entry(country="Israel", region="Tel-Aviv District")])
    city = _city(
        city_id="tel-aviv--tel-aviv-district--israel",
        city_name="Tel-Aviv",
        state_name="Tel-Aviv District",
        country_name="Israel",
    )
    tier, matched = plan_match.match_city(city, index)
    assert tier == "region"
    assert len(matched) == 1


# ── Summarizing ────────────────────────────────────────────────────────────


def test_an_active_entry_excludes_closed_ones_from_the_window():
    # A closed 2019 campaign beside a live 2026 one must not stretch the
    # reported window back seven years.
    entries = [
        _entry(publish="No", date_start="2019-01-01", date_end="2019-06-01"),
        _entry(publish="Yes", date_start="2026-04-13", date_end="2026-11-01"),
    ]
    summary = plan_match.summarize_entries(entries)
    assert summary.active_count == 1
    assert summary.window_start == "2026-04-13"
    assert summary.window_end == "2026-11-01"


def test_with_nothing_active_the_span_covers_every_entry():
    entries = [
        _entry(publish="No", date_start="2019-01-01", date_end="2019-03-01"),
        _entry(publish="No", date_start="2018-11-01", date_end="2019-06-01"),
    ]
    summary = plan_match.summarize_entries(entries)
    assert summary.is_active is False
    assert summary.window_start == "2018-11-01"
    assert summary.window_end == "2019-06-01"


def test_publish_flag_matching_is_case_and_whitespace_tolerant():
    assert plan_match.summarize_entries([_entry(publish=" yes ")]).active_count == 1


def test_record_key_groups_an_exploded_record_back_together():
    a, b = _entry(district="Ada"), _entry(district="Adams")
    assert plan_match.record_key(a) == plan_match.record_key(b)
    assert plan_match.record_key(a) != plan_match.record_key(_entry(region="Oregon"))


# ── Verdicts ───────────────────────────────────────────────────────────────


def test_israel_is_driven_unplanned_the_finding_the_page_exists_for():
    # Feed: campaign closed Feb 2019. Our runs: captures in Oct 2023.
    summary = plan_match.summarize_entries(
        [
            _entry(
                country="Israel",
                publish="No",
                date_start=None,
                date_start_raw="14/2/19",
                date_end=None,
                date_end_raw="28/2/19",
            )
        ]
    )
    verdict = plan_match.classify(summary, date(2023, 10, 1), TODAY)
    assert verdict == plan_match.VERDICT_DRIVEN_UNPLANNED


def test_imagery_inside_an_archived_window_confirms_the_drive():
    summary = plan_match.summarize_entries([_entry()])
    assert (
        plan_match.classify(summary, date(2026, 7, 1), TODAY) == plan_match.VERDICT_DRIVE_CONFIRMED
    )


def test_an_open_window_with_older_imagery_is_still_planned_open():
    summary = plan_match.summarize_entries([_entry()])
    assert plan_match.classify(summary, date(2024, 1, 1), TODAY) == plan_match.VERDICT_PLANNED_OPEN


def test_a_future_window_reads_as_upcoming():
    summary = plan_match.summarize_entries([_entry(date_start="2027-04-01", date_end="2027-11-01")])
    assert (
        plan_match.classify(summary, date(2024, 1, 1), TODAY) == plan_match.VERDICT_PLANNED_UPCOMING
    )


def test_no_match_is_not_listed():
    assert plan_match.classify(None, date(2024, 1, 1), TODAY) == plan_match.VERDICT_NOT_LISTED


def test_imagery_just_after_a_window_is_not_called_unplanned():
    # Within the slack, a window that slipped or was quietly extended is the
    # ordinary explanation, not a secret drive.
    summary = plan_match.summarize_entries(
        [_entry(publish="No", date_start="2026-01-01", date_end="2026-02-01")]
    )
    assert plan_match.classify(summary, date(2026, 3, 1), TODAY) == plan_match.VERDICT_CLOSED


def test_a_closed_campaign_with_no_imagery_at_all_is_just_closed():
    summary = plan_match.summarize_entries([_entry(publish="No")])
    assert plan_match.classify(summary, None, TODAY) == plan_match.VERDICT_CLOSED


# ── Implausible capture dates ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "2611-09-01T00:00:00",  # real production values, from corrupt EXIF
        "2612-01-01T00:00:00",
        "1970-08-01T00:00:00",  # predates Street View entirely
        "1980-01-01T00:00:00",
        "2026-11-01T00:00:00",  # in the future relative to TODAY
    ],
)
def test_impossible_capture_dates_are_treated_as_absent(value):
    # runs.newest_capture_date is computed over EVERY pano including
    # third-party photospheres, so one corrupt EXIF poisons a whole city. A
    # 2611 date would manufacture a driven_unplanned verdict out of a typo.
    assert plan_match.plausible_capture_date(value, TODAY) is None


@pytest.mark.parametrize("value", ["2023-10-01T00:00:00", "2007-01-01", "2026-08-16"])
def test_plausible_capture_dates_survive(value):
    assert plan_match.plausible_capture_date(value, TODAY) is not None


def test_a_suppressed_capture_date_cannot_produce_a_driven_unplanned_verdict():
    summary = plan_match.summarize_entries([_entry(publish="No", date_end="2019-06-01")])
    corrupt = plan_match.plausible_capture_date("2611-09-01T00:00:00", TODAY)
    assert plan_match.classify(summary, corrupt, TODAY) == plan_match.VERDICT_CLOSED


# ── Revision diffs ─────────────────────────────────────────────────────────


def test_a_closed_campaign_is_reported_as_a_mutation_not_a_delete_plus_insert():
    # Keyed by record, a publish flip reads as one record vanishing and another
    # appearing, which is true but useless. Grouped by region it reads as what
    # it is: this campaign closed.
    before = [_entry(region="Santa Fe", district="Rosario", publish="Yes")]
    after = [_entry(region="Santa Fe", district="Rosario", publish="No")]
    diff = plan_match.diff_snapshots(before, after)

    assert diff["campaigns_closed"] == 1
    assert diff["regions_added"] == 0 and diff["regions_removed"] == 0
    assert diff["detail"]["closed"][0]["region"] == "Santa Fe"


def test_a_reopened_campaign_is_distinguished_from_a_closed_one():
    before = [_entry(publish="No")]
    after = [_entry(publish="Yes")]
    diff = plan_match.diff_snapshots(before, after)
    assert diff["campaigns_reopened"] == 1
    assert diff["campaigns_closed"] == 0


def test_the_ibraltar_corruption_reads_as_a_district_change_on_one_region():
    # The real 2026-08-11 revision: Google replaced Austria/Steiermark's twenty
    # districts with the single string "ibraltar" — "Gibraltar" minus its first
    # character — and left it live. The archive existing is the only reason
    # this is observable at all.
    before = [
        _entry(country="Austria", region="Steiermark", district=d)
        for d in ("Graz", "Leoben", "Liezen", "Murau", "Voitsberg")
    ]
    after = [_entry(country="Austria", region="Steiermark", district="ibraltar")]
    diff = plan_match.diff_snapshots(before, after)

    assert diff["districts_changed"] == 1
    assert diff["regions_removed"] == 0, "the region survived; only its districts were rewritten"
    detail = diff["detail"]["districts"][0]
    assert detail["region"] == "Steiermark"
    assert detail["lost_count"] == 5
    assert detail["gained"] == ["ibraltar"]


def test_a_shifted_window_is_reported_without_touching_the_district_count():
    before = [_entry(date_start="2026-04-13", date_end="2026-11-01")]
    after = [_entry(date_start="2026-05-01", date_end="2026-12-01")]
    diff = plan_match.diff_snapshots(before, after)
    assert diff["windows_changed"] == 1
    assert diff["districts_changed"] == 0
    assert diff["campaigns_closed"] == 0


def test_a_multi_window_regions_span_covers_all_of_its_campaigns():
    # Idaho and Oregon each carry an ACTIVE 2026 window beside a closed 2025-12
    # one. Flattening the (start, end) pairs and taking the two globally
    # smallest values published the dead window and dropped the live one, so
    # the revision log reported the wrong campaign as the region's window.
    # The span is [earliest start, latest end], taken from the right halves.
    before = [_entry(date_start="2025-04-01", date_end="2025-12-01")]
    after = [
        _entry(district="Ada", date_start="2025-04-01", date_end="2025-12-01"),
        _entry(district="Adams", date_start="2026-04-13", date_end="2026-11-01"),
    ]
    diff = plan_match.diff_snapshots(before, after)

    assert diff["windows_changed"] == 1
    detail = diff["detail"]["windows"][0]
    assert detail["from"] == ["2025-04-01", "2025-12-01"]
    assert detail["to"] == ["2025-04-01", "2026-11-01"], "must reach the live window's end"


def test_a_window_span_never_pairs_two_campaigns_start_dates():
    # With ends dirty beyond even parse_loose_date's day-first recovery (the
    # feed's truncated month names), the old flatten-and-slice took the two
    # smallest SURVIVING values — here two different campaigns' starts — and
    # published them as a from→to range: a window that never existed. A span
    # with no usable end is now simply a null end.
    before = [_entry(date_start="2019-01-01", date_end="2019-06-01")]
    after = [
        _entry(district="Ada", date_start="2026-04-13", date_end=None, date_end_raw="Sept"),
        _entry(district="Adams", date_start="2026-05-01", date_end=None, date_end_raw=None),
    ]
    diff = plan_match.diff_snapshots(before, after)

    to_span = diff["detail"]["windows"][0]["to"]
    assert to_span[0] == "2026-04-13"
    assert to_span[1] is None, "no usable end date is a null end, never another start"


def test_a_wholly_new_or_dropped_region_is_counted_as_such():
    before = [_entry(region="Idaho")]
    after = [_entry(region="Oregon")]
    diff = plan_match.diff_snapshots(before, after)
    assert diff["regions_added"] == 1
    assert diff["regions_removed"] == 1
    assert diff["districts_changed"] == 0


def test_an_unchanged_pair_reports_nothing():
    entries = [_entry(district="Ada"), _entry(district="Adams")]
    diff = plan_match.diff_snapshots(entries, list(entries))
    assert all(
        diff[k] == 0
        for k in (
            "regions_added",
            "regions_removed",
            "campaigns_closed",
            "campaigns_reopened",
            "windows_changed",
            "districts_changed",
        )
    )


def test_detail_lists_are_capped_but_counters_stay_exact():
    # A feed-wide edit must not republish the feed. The counter is the number
    # that gets quoted; the examples are only illustrative.
    before = [_entry(region=f"Region {i}", district="X", publish="Yes") for i in range(60)]
    after = [_entry(region=f"Region {i}", district="X", publish="No") for i in range(60)]
    diff = plan_match.diff_snapshots(before, after)

    assert diff["campaigns_closed"] == 60
    assert len(diff["detail"]["closed"]) == plan_match._MAX_DETAIL


def test_region_matching_in_diffs_is_normalized_like_everywhere_else():
    # "State of Vienna" vs "Vienna" is the same region; a diff must not report
    # it as one added and one removed.
    before = [_entry(country="Austria", region="State of Vienna", district="Wien")]
    after = [_entry(country="Austria", region="Vienna", district="Wien")]
    diff = plan_match.diff_snapshots(before, after)
    assert diff["regions_added"] == 0
    assert diff["regions_removed"] == 0
