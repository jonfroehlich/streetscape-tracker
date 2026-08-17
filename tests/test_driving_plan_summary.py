"""
Tests for `generate_driving_plan_summary` — the published join of Google's
driving plan against observed imagery (issue #176).

The artifact is the one thing this project publishes that is derived from a
third party's content, so its contract matters in two directions: it must carry
enough for the page to state a verdict AND enough for a reader to re-derive it,
and it must never present a heuristic or a corrupt catalog value as fact.
"""

import gzip
import json
import os
from datetime import date

import pytest

from streetscape_metadata_tracker import db, plan_match
from streetscape_metadata_tracker.json_summarizer import (
    _compact_capture_years,
    generate_driving_plan_summary,
)


def strict_load(path):
    """json.load that raises on NaN/Infinity literals (JSON.parse rejects them)."""

    def _reject(token):
        raise ValueError(f"invalid token {token}")

    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f, parse_constant=_reject)


def _artifact(data_dir):
    return strict_load(os.path.join(data_dir, "driving_plan.json.gz"))


def _register_city(conn, name, state, country="United States", code="US"):
    return db.register_city(
        conn,
        city_name=name,
        state_name=state,
        state_code=None,
        country_name=country,
        country_code=code,
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=800,
        grid_height_m=800,
        step_m=20,
    )


def _snapshot(conn, fetch_date="2026-08-11", changed=True):
    return db.register_driving_plan_snapshot(
        conn,
        fetch_date=date.fromisoformat(fetch_date),
        sha256="deadbeef" + fetch_date,
        record_count=1,
        changed=changed,
        artifact_filename=f"gsv_driving_plan_{fetch_date}.json.gz" if changed else None,
    )


def _entries(conn, snapshot_id, rows):
    """
    rows: (country, region, district, publish, start, start_raw, end, end_raw).

    Note the catalog's column order puts each RAW value before its parsed one
    (``date_start_raw, date_start``), which is the opposite of how the rows
    read here — hence the reordering below rather than a straight splat.
    """
    db.replace_driving_plan_entries(
        conn,
        snapshot_id,
        [
            (snapshot_id, country, "US", "SV", region, district, publish, s_raw, s, e_raw, e)
            for (country, region, district, publish, s, s_raw, e, e_raw) in rows
        ],
    )


IDAHO = (
    "United States",
    "Idaho",
    "Ada",
    "Yes",
    "2026-04-13",
    "2026-04-13T07:00:00.000Z",
    "2026-11-01",
    "2026-11-01T07:00:00.000Z",
)


def test_artifact_is_written_and_matches_the_returned_dict(conn, data_dir):
    city_id = _register_city(conn, "Boise", "Idaho")
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 20),
        csv_filename="boise_2026-07-20.csv.gz",
        coverage_rate_pct=61.0,
        unique_google_panos=1234,
        newest_capture_date="2026-07-01T00:00:00",
    )
    snap = _snapshot(conn)
    _entries(conn, snap, [IDAHO])

    doc = generate_driving_plan_summary(conn, data_dir)

    assert doc["schema_version"] == 1
    assert "generated_at" in doc
    assert _artifact(data_dir) == doc


def test_empty_catalog_still_writes_a_file(conn, data_dir):
    # The page fetches this unconditionally; a missing file is an error state,
    # an empty one renders as "nothing published yet".
    doc = generate_driving_plan_summary(conn, data_dir)
    assert doc["cities"] == []
    assert doc["records"] == []
    assert _artifact(data_dir)["schema_version"] == 1


def test_plan_and_observed_are_absent_not_null(conn, data_dir):
    # An all-null block is TRUTHY, so no `if (rec.plan)` consumer can reject
    # it — which is exactly what the absent-not-null convention exists to
    # prevent.
    listed = _register_city(conn, "Boise", "Idaho")
    db.register_run(
        conn,
        city_id=listed,
        run_date=date(2026, 7, 20),
        csv_filename="boise_2026-07-20.csv.gz",
        coverage_rate_pct=61.0,
    )
    _register_city(conn, "Addis Ababa", "Addis Ababa", country="Ethiopia", code="ET")

    snap = _snapshot(conn)
    _entries(conn, snap, [IDAHO])

    doc = generate_driving_plan_summary(conn, data_dir)
    by_id = {c["city_id"]: c for c in doc["cities"]}

    boise = by_id[listed]
    assert "plan" in boise and "observed" in boise

    addis = next(c for cid, c in by_id.items() if "addis" in cid)
    assert "plan" not in addis, "an unlisted city must carry no plan key at all"
    assert "observed" not in addis, "a never-collected city must carry no observed key"
    assert addis["verdict"] == "not_listed"


def test_israel_reads_as_driven_unplanned_end_to_end(conn, data_dir):
    # The motivating case, through the real generator: the feed says the
    # campaign closed in Feb 2019 (with dirty dates), our run says Oct 2023.
    city_id = _register_city(conn, "Tel-Aviv", "Tel-Aviv District", country="Israel", code="IL")
    assert city_id == "tel-aviv--tel-aviv-district--israel", "the manual link keys on this id"
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 8, 12),
        csv_filename="tel-aviv_2026-08-12.csv.gz",
        coverage_rate_pct=57.9,
        unique_google_panos=101975,
        newest_capture_date="2023-10-01T00:00:00",
    )
    snap = _snapshot(conn)
    _entries(
        conn,
        snap,
        [("Israel", "תל אביב", "תל אביב", "No", None, "14/2/19", None, "28/2/19")],
    )

    doc = generate_driving_plan_summary(conn, data_dir)
    city = doc["cities"][0]

    assert city["verdict"] == "driven_unplanned"
    assert city["plan"]["match_tier"] == "manual"
    assert city["plan"]["active_count"] == 0
    # Recovered from the dirty raw values, and flagged as such.
    assert city["plan"]["window_end"] == "2019-02-28"
    assert city["plan"]["window_approximate"] is True
    assert city["observed"]["gsv"]["newest_capture"] == "2023-10-01"


def test_a_clean_window_carries_no_approximate_flag(conn, data_dir):
    city_id = _register_city(conn, "Boise", "Idaho")
    db.register_run(conn, city_id=city_id, run_date=date(2026, 7, 20), csv_filename="b.csv.gz")
    _entries(conn, _snapshot(conn), [IDAHO])
    doc = generate_driving_plan_summary(conn, data_dir)
    assert "window_approximate" not in doc["cities"][0]["plan"]


def test_impossible_capture_dates_are_suppressed_not_published(conn, data_dir):
    # 22 production runs read 2611-2612 because the capture-date columns are
    # computed over third-party photospheres too. Publishing one would both
    # look absurd and manufacture a driven_unplanned verdict from a typo.
    #
    # median_pano_age_years goes with it: it is derived from the SAME panos, so
    # a row carrying 2611 carries -292.0 beside it, and blanking one column
    # while the other renders "-292.0 yrs" leaves the page self-contradicting.
    city_id = _register_city(conn, "Chicago", "Illinois")
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 30),
        csv_filename="chicago_2026-07-30.csv.gz",
        coverage_rate_pct=70.0,
        newest_capture_date="2611-09-01T00:00:00",
        median_pano_age_years=-292.0,
    )
    _entries(
        conn,
        _snapshot(conn),
        [("United States", "Illinois", "Cook", "No", "2019-01-01", "x", "2019-06-01", "x")],
    )

    doc = generate_driving_plan_summary(conn, data_dir)
    observed = doc["cities"][0]["observed"]["gsv"]

    assert observed["newest_capture"] is None
    assert observed["median_pano_age_years"] is None
    assert "years_since_newest_capture" not in observed
    assert doc["cities"][0]["verdict"] != "driven_unplanned"


def test_a_negative_median_age_is_suppressed_on_its_own_terms(conn, data_dir):
    """A corrupt date can poison the median without owning the maximum.

    The guard above keys on newest_capture_date, which is the column a 2611
    pano usually captures — but a run whose newest date is ordinary can still
    carry a median dragged negative by a future-dated pano that lost the max to
    an even later one, or by a pre-repair row whose columns disagree. An age
    measured from the run date cannot be negative: nothing is captured after
    the query that observed it.
    """
    city_id = _register_city(conn, "Denver", "Colorado")
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 30),
        csv_filename="denver_2026-07-30.csv.gz",
        coverage_rate_pct=70.0,
        newest_capture_date="2024-05-01T00:00:00",
        median_pano_age_years=-3.5,
    )
    observed = generate_driving_plan_summary(conn, data_dir)["cities"][0]["observed"]["gsv"]

    # The plausible max survives; only the impossible median is withheld.
    assert observed["newest_capture"] == "2024-05-01"
    assert observed["median_pano_age_years"] is None


def test_a_healthy_median_is_published_untouched(conn, data_dir):
    """The guards above are exception paths, not a filter on ordinary rows."""
    city_id = _register_city(conn, "Boise", "Idaho")
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 30),
        csv_filename="boise_2026-07-30.csv.gz",
        coverage_rate_pct=70.0,
        newest_capture_date="2024-05-01T00:00:00",
        median_pano_age_years=3.5,
    )
    observed = generate_driving_plan_summary(conn, data_dir)["cities"][0]["observed"]["gsv"]

    assert observed["newest_capture"] == "2024-05-01"
    assert observed["median_pano_age_years"] == 3.5


def test_records_are_grouped_by_feed_record_not_exploded_districts(conn, data_dir):
    # The catalog stores one row per (record, district); regrouping keeps the
    # published collection at ~3.7k rows instead of ~11.7k, which matters
    # because the table chassis renders every matching row on each keystroke.
    snap = _snapshot(conn)
    _entries(
        conn,
        snap,
        [
            ("United States", "Idaho", d, "Yes", "2026-04-13", "x", "2026-11-01", "x")
            for d in ("Ada", "Adams", "Bannock", "Boise")
        ],
    )
    doc = generate_driving_plan_summary(conn, data_dir)

    assert len(doc["records"]) == 1
    record = doc["records"][0]
    assert record["district_count"] == 4
    assert sorted(record["districts"]) == ["Ada", "Adams", "Bannock", "Boise"]


def test_records_advertise_which_tracked_cities_they_cover(conn, data_dir):
    # The discovery direction: a record covering no city is a collection
    # target, and that is only computable while the match is in hand.
    boise = _register_city(conn, "Boise", "Idaho")
    _register_city(conn, "Salem", "Oregon")
    snap = _snapshot(conn)
    _entries(
        conn,
        snap,
        [
            IDAHO,
            ("Argentina", "Chubut", "Esquel", "Yes", "2026-01-01", "x", "2026-12-31", "x"),
        ],
    )

    doc = generate_driving_plan_summary(conn, data_dir)
    by_region = {r["region"]: r for r in doc["records"]}

    assert by_region["Idaho"]["matched_city_ids"] == [boise]
    assert by_region["Idaho"]["matched_city_count"] == 1
    assert by_region["Chubut"]["matched_city_count"] == 0


def test_a_country_only_match_does_not_claim_a_record_covers_the_city(conn, data_dir):
    # Salem, Oregon matches the US plan only at country level, because the feed
    # carries no Oregon record here. It must NOT land in Idaho's matched list:
    # doing so both misattributes the city and erases genuine "no tracked city"
    # records from the collection-target list, since almost every record in a
    # country we collect anywhere would pick up a spurious match.
    salem = _register_city(conn, "Salem", "Oregon")
    _entries(conn, _snapshot(conn), [IDAHO])

    doc = generate_driving_plan_summary(conn, data_dir)

    assert doc["records"][0]["matched_city_ids"] == []
    assert doc["records"][0]["matched_city_count"] == 0
    # The city still gets a plan block — its country IS in the plan — but the
    # tier says exactly how weak the link is.
    city = next(c for c in doc["cities"] if c["city_id"] == salem)
    assert city["plan"]["match_tier"] == "country"


def test_country_aliases_are_applied_to_published_records(conn, data_dir):
    # Without normalization a Brazilian city reads as "not in plan" while its
    # entries sit in the feed under "Brasil".
    city_id = _register_city(conn, "Goiania", "Goias", country="Brazil", code="BR")
    _entries(
        conn,
        _snapshot(conn),
        [("Brasil", "Goias", "Goiania", "Yes", "2026-01-01", "x", "2026-12-31", "x")],
    )
    doc = generate_driving_plan_summary(conn, data_dir)

    assert doc["records"][0]["country_matched"] == "Brazil"
    city = next(c for c in doc["cities"] if c["city_id"] == city_id)
    assert "plan" in city
    assert city["verdict"] != "not_listed"


def test_observations_carry_the_filename_the_city_page_is_addressed_by(conn, data_dir):
    # city.html is reached by run filename, not city_id, so a row cannot link
    # out without it.
    city_id = _register_city(conn, "Boise", "Idaho")
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 20),
        csv_filename="boise_width_1_height_1_step_20_2026-07-20.csv.gz",
    )
    db.register_run(
        conn,
        city_id=city_id,
        provider="mapillary",
        run_date=date(2026, 7, 20),
        csv_filename="boise_width_1_height_1_step_20_mapillary_2026-07-20.csv.gz",
    )
    doc = generate_driving_plan_summary(conn, data_dir)
    observed = doc["cities"][0]["observed"]

    assert observed["gsv"]["csv_filename"].endswith("2026-07-20.csv.gz")
    assert "mapillary" in observed["mapillary"]["csv_filename"]


def test_plan_meta_reports_how_thin_the_archive_still_is(conn, data_dir):
    # The page's principal caveat. "We have looked 8 times since 2026-07-31"
    # is what stops a reader reading an empty plan cell as a statement about
    # Google's intentions in 2023.
    _snapshot(conn, "2026-07-31", changed=True)
    _snapshot(conn, "2026-08-05", changed=True)
    snap = _snapshot(conn, "2026-08-11", changed=True)
    _snapshot(conn, "2026-08-16", changed=False)
    _entries(conn, snap, [IDAHO])

    meta = generate_driving_plan_summary(conn, data_dir)["plan"]

    assert meta["fetch_count"] == 4
    assert meta["change_count"] == 3
    assert meta["first_fetch"] == "2026-07-31"
    assert meta["latest_fetch"] == "2026-08-16"
    assert meta["latest_change"] == "2026-08-11"
    assert meta["source_url"].startswith("https://")
    assert "driving distance" in meta["disclaimer"]


def test_entries_come_from_the_latest_changed_snapshot(conn, data_dir):
    # Unchanged fetches write a snapshot row but no entries, so reading "the
    # latest snapshot" rather than "the latest CHANGED one" would publish an
    # empty plan on most nights.
    old = _snapshot(conn, "2026-08-05", changed=True)
    _entries(conn, old, [("United States", "Oregon", "Lane", "No", None, "x", None, "x")])
    new = _snapshot(conn, "2026-08-11", changed=True)
    _entries(conn, new, [IDAHO])
    _snapshot(conn, "2026-08-16", changed=False)

    doc = generate_driving_plan_summary(conn, data_dir)

    assert [r["region"] for r in doc["records"]] == ["Idaho"]


def test_records_carry_a_stable_id_and_their_own_verdict(conn, data_dir):
    # Plan records become rows on the Driving page, so they need an id the
    # table can sort on and a verdict from the SAME classify() the city rows
    # use — reimplementing the vocabulary in JavaScript would let the two
    # drift.
    _entries(conn, _snapshot(conn), [IDAHO])
    doc = generate_driving_plan_summary(conn, data_dir)
    record = doc["records"][0]

    assert record["record_id"].startswith("plan:")
    assert record["verdict"] == "planned_open"

    # Stable across regeneration: an ordinal would shift the moment Google adds
    # a record, breaking every deep link.
    again = generate_driving_plan_summary(conn, data_dir)
    assert again["records"][0]["record_id"] == record["record_id"]


def test_a_record_can_never_be_drive_confirmed_since_it_has_no_imagery(conn, data_dir):
    # There is nothing observed to weigh against a record, so its verdict can
    # only ever be a plan status.
    _entries(
        conn,
        _snapshot(conn),
        [("United States", "Illinois", "Cook", "No", "2019-01-01", "x", "2019-06-01", "x")],
    )
    doc = generate_driving_plan_summary(conn, data_dir)
    assert doc["records"][0]["verdict"] == "closed"


# ── Capture-year histogram ─────────────────────────────────────────────────


def _write_run_json(data_dir, filename, year_counts, block="google_panos", nested=True):
    # `nested` selects between the two per-run JSON generations that are both
    # on disk in the real archive — see test_both_histogram_shapes_are_read.
    hist = {"counts": year_counts} if nested else dict(year_counts)
    payload = {block: {"histogram_of_capture_dates_by_year": hist}}
    with gzip.open(os.path.join(data_dir, filename), "wt", encoding="utf-8") as f:
        json.dump(payload, f)


def test_capture_years_are_published_in_a_dense_form(conn, data_dir):
    city_id = _register_city(conn, "Boise", "Idaho")
    _write_run_json(data_dir, "boise.json.gz", {"2019": 12, "2021": 4, "2022": 900})
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 20),
        csv_filename="boise.csv.gz",
        json_filename="boise.json.gz",
    )
    _entries(conn, _snapshot(conn), [IDAHO])

    doc = generate_driving_plan_summary(conn, data_dir)

    # [first_year, [counts…]] — the year keys are named once, and 2020's zero
    # is kept because a gap between drives is itself an observation.
    assert doc["cities"][0]["capture_years"] == [2019, [12, 0, 4, 900]]


@pytest.mark.parametrize("nested", [True, False])
def test_both_histogram_shapes_are_read(conn, data_dir, nested):
    """
    Per-run JSONs come in two generations and BOTH are on disk:

        newer:  {"histogram_of_capture_dates_by_year": {"counts": {...}}}
        older:  {"histogram_of_capture_dates_by_year": {"2008": 3, ...}}

    Reading only the nested form silently dropped the capture history for 178
    of 1,144 catalogued cities. Run files are immutable dated snapshots, so the
    old shape never goes away and has to be read rather than migrated.
    """
    city_id = _register_city(conn, "Aberdeen", "Washington")
    _write_run_json(data_dir, "abd.json.gz", {"2019": 3, "2020": 7}, nested=nested)
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 20),
        csv_filename="abd.csv.gz",
        json_filename="abd.json.gz",
    )
    doc = generate_driving_plan_summary(conn, data_dir)
    assert doc["cities"][0]["capture_years"] == [2019, [3, 7]]


def test_capture_years_come_from_the_google_only_block(conn, data_dir):
    # all_panos includes third-party photospheres, whose corrupt EXIF is
    # issue #213. google_panos is already copyright-filtered, which makes the
    # sparkline more trustworthy than the newest_capture column beside it.
    city_id = _register_city(conn, "Boise", "Idaho")
    with gzip.open(os.path.join(data_dir, "boise.json.gz"), "wt", encoding="utf-8") as f:
        json.dump(
            {
                "all_panos": {"histogram_of_capture_dates_by_year": {"counts": {"1970": 5}}},
                "google_panos": {"histogram_of_capture_dates_by_year": {"counts": {"2022": 7}}},
            },
            f,
        )
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 20),
        csv_filename="boise.csv.gz",
        json_filename="boise.json.gz",
    )
    doc = generate_driving_plan_summary(conn, data_dir)
    assert doc["cities"][0]["capture_years"] == [2022, [7]]


def test_capture_years_are_absent_not_null_when_unavailable(conn, data_dir):
    # No run, no JSON on disk, and an empty histogram must all read the same
    # way to the frontend: the key simply is not there.
    _register_city(conn, "Nowhere", "Idaho")
    listed = _register_city(conn, "Ghost", "Idaho")
    db.register_run(
        conn,
        city_id=listed,
        run_date=date(2026, 7, 20),
        csv_filename="ghost.csv.gz",
        json_filename="missing-from-disk.json.gz",
    )
    doc = generate_driving_plan_summary(conn, data_dir)
    assert all("capture_years" not in c for c in doc["cities"])


def test_an_absurd_capture_year_is_dropped_without_erasing_the_histogram(conn, data_dir):
    # A 2611 year (issue #213) would otherwise produce a 600-element array per
    # city. It is dropped INDIVIDUALLY: one corrupt bucket is exactly what #213
    # looks like, and refusing the whole histogram would let a single bad year
    # erase a city's entire real capture history.
    city_id = _register_city(conn, "Chicago", "Illinois")
    _write_run_json(data_dir, "chi.json.gz", {"2019": 10, "2020": 3, "2611": 1})
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 30),
        csv_filename="chi.csv.gz",
        json_filename="chi.json.gz",
    )
    doc = generate_driving_plan_summary(conn, data_dir)
    assert doc["cities"][0]["capture_years"] == [2019, [10, 3]]


def test_capture_years_are_filtered_by_the_projects_one_plausibility_window():
    # Both ends of plan_match's window and nothing else: a year before Street
    # View existed and a year after `today` are dropped, the years inside are
    # kept. Reusing that bound rather than a second span rule is the point —
    # two plausibility rules in one codebase drift apart. Unit-level because
    # `today` is where the upper bound enters, and pinning it keeps the
    # expected array from changing shape as real time passes.
    counts = {"1970": 5, "2006": 5, "2007": 2, "2026": 4, "2027": 9}
    first, dense = _compact_capture_years(counts, date(2026, 7, 30))
    assert first == plan_match.EARLIEST_PLAUSIBLE_CAPTURE.year == 2007
    assert len(dense) == 2026 - 2007 + 1
    assert dense[0] == 2 and dense[-1] == 4
    assert sum(dense) == 6  # 1970, 2006 and 2027 contributed nothing


def test_a_histogram_of_only_absurd_years_is_absent_not_empty(conn, data_dir):
    # Nothing plausible left is the same fact as "no histogram" — the key must
    # not appear as an empty array the frontend would try to plot.
    city_id = _register_city(conn, "Phoenix", "Arizona")
    _write_run_json(data_dir, "phx.json.gz", {"1970": 5, "2611": 1})
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 7, 30),
        csv_filename="phx.csv.gz",
        json_filename="phx.json.gz",
    )
    doc = generate_driving_plan_summary(conn, data_dir)
    assert "capture_years" not in doc["cities"][0]


# ── Revisions ──────────────────────────────────────────────────────────────


def test_revisions_diff_consecutive_changed_snapshots(conn, data_dir):
    # Entries exist only for changed snapshots, so consecutive changed
    # snapshots are exactly the comparable pairs — an unchanged fetch in
    # between must not create a phantom revision.
    first = _snapshot(conn, "2026-08-05", changed=True)
    _entries(
        conn,
        first,
        [("United States", "Idaho", "Ada", "Yes", "2026-04-13", "x", "2026-11-01", "x")],
    )
    _snapshot(conn, "2026-08-08", changed=False)  # no entries, must be skipped
    second = _snapshot(conn, "2026-08-11", changed=True)
    _entries(
        conn,
        second,
        [("United States", "Idaho", "Ada", "No", "2026-04-13", "x", "2026-11-01", "x")],
    )

    doc = generate_driving_plan_summary(conn, data_dir)

    assert len(doc["revisions"]) == 1
    revision = doc["revisions"][0]
    assert revision["from"] == "2026-08-05"
    assert revision["to"] == "2026-08-11"
    assert revision["campaigns_closed"] == 1


def test_a_single_snapshot_yields_no_revisions(conn, data_dir):
    # Nothing to compare against yet — an empty list, not a fabricated entry.
    _entries(conn, _snapshot(conn), [IDAHO])
    assert generate_driving_plan_summary(conn, data_dir)["revisions"] == []


def test_revisions_are_newest_first_and_bounded(conn, data_dir):
    for i, day in enumerate(range(1, 6)):
        snap = _snapshot(conn, f"2026-08-0{day}", changed=True)
        _entries(
            conn,
            snap,
            [("United States", f"Region {i}", "X", "Yes", "2026-01-01", "x", "2026-12-31", "x")],
        )
    doc = generate_driving_plan_summary(conn, data_dir)

    dates = [r["to"] for r in doc["revisions"]]
    assert dates == sorted(dates, reverse=True), "newest revision first"
    assert len(doc["revisions"]) == 4  # 5 snapshots -> 4 consecutive pairs


def test_a_disabled_city_is_published_and_flagged(conn, data_dir):
    # Registered-but-disabled cities still belong in the table (the plan may
    # be about to drive them); the page filters on this flag rather than the
    # artifact hiding them.
    db.register_city(
        conn,
        city_name="Sleepy",
        state_name="Idaho",
        state_code=None,
        country_name="United States",
        country_code="US",
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=800,
        grid_height_m=800,
        step_m=20,
        enabled=False,
    )
    _entries(conn, _snapshot(conn), [IDAHO])
    doc = generate_driving_plan_summary(conn, data_dir)
    assert doc["cities"][0]["enabled"] is False
