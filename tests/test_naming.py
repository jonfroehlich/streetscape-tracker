"""Filename convention tests: all filename generations must parse and round-trip."""

from datetime import date

import pytest

from streetscape_metadata_tracker.naming import (
    generate_base_filename,
    generate_run_filename,
    generate_streetwalk_diff_filename,
    generate_streetwalk_filename,
    parse_filename,
    parse_streetwalk_filename,
    same_grid_geometry,
    sanitize_city_query_str,
    streets_filename_for_run,
    streetwalk_coverage_filename,
)


def test_parse_legacy_int_name():
    p = parse_filename("grand-marais--mn--usa_width_1000_height_1000_step_20.csv.gz")
    assert p.slug == "grand-marais--mn--usa"
    assert p.city_query_str == "Grand Marais, Mn, Usa"
    assert (p.width_meters, p.height_meters, p.step_meters) == (1000, 1000, 20)
    assert p.run_date is None
    assert p.provider == "gsv"


def test_parse_buggy_float_step_name():
    p = parse_filename("bend--or_width_5000_height_5000_step_20.0.csv.gz")
    assert p.step_meters == 20
    assert p.run_date is None
    assert p.provider == "gsv"


def test_parse_dated_name():
    p = parse_filename(
        "bend--oregon--united-states_width_5000_height_5000_step_20_2026-07-02.json.gz"
    )
    assert p.run_date == date(2026, 7, 2)
    assert p.slug == "bend--oregon--united-states"
    assert p.provider == "gsv"


def test_parse_legacy_single_underscore_slug():
    p = parse_filename("amsterdam_nl_width_20000_height_20000_step_20.csv.gz")
    assert p.slug == "amsterdam_nl"
    assert p.width_meters == 20000


def test_parse_full_path_and_extensions():
    for ext in (".csv.gz", ".json.gz", ".csv", ".json", ".html"):
        p = parse_filename(f"/some/dir/x--y_width_10_height_20_step_5{ext}")
        assert (p.width_meters, p.height_meters, p.step_meters) == (10, 20, 5)


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_filename("cities.json.gz")


def test_generate_base_filename_int_casts_step():
    # Regression: float --step used to produce unparseable `_step_20.0` names
    name = generate_base_filename("Bend, OR", 5000.0, 5000.0, 20.0)
    assert name == "bend--or_width_5000_height_5000_step_20"
    parse_filename(name + ".csv.gz")  # must round-trip


def test_generate_run_filename_roundtrip():
    name = generate_run_filename("bend--oregon--united-states", 5000, 5000, 20, date(2026, 7, 2))
    p = parse_filename(name + ".csv.gz")
    assert p.slug == "bend--oregon--united-states"
    assert p.run_date == date(2026, 7, 2)
    assert p.provider == "gsv"


def test_generate_run_filename_gsv_has_no_provider_token():
    # Explicit provider='gsv' must produce byte-identical names to the
    # pre-provider convention (published URLs depend on this).
    assert generate_run_filename(
        "bend--or", 5000, 5000, 20, date(2026, 7, 2), provider="gsv"
    ) == generate_run_filename("bend--or", 5000, 5000, 20, date(2026, 7, 2))


def test_parse_mapillary_dated_name():
    p = parse_filename(
        "bend--oregon--united-states_width_5000_height_5000_step_20_mapillary_2026-07-05.csv.gz"
    )
    assert p.provider == "mapillary"
    assert p.run_date == date(2026, 7, 5)
    assert p.slug == "bend--oregon--united-states"
    assert (p.width_meters, p.height_meters, p.step_meters) == (5000, 5000, 20)


def test_parse_mapillary_with_float_step():
    p = parse_filename("bend--or_width_5000_height_5000_step_20.0_mapillary_2026-07-05.csv.gz")
    assert p.provider == "mapillary"
    assert p.step_meters == 20


def test_generate_mapillary_run_filename_roundtrip():
    name = generate_run_filename(
        "st.-louis--mo--usa", 1000, 1000, 20, date(2026, 7, 5), provider="mapillary"
    )
    assert name == "st.-louis--mo--usa_width_1000_height_1000_step_20_mapillary_2026-07-05"
    p = parse_filename(name + ".csv.gz")
    assert p.provider == "mapillary"
    assert p.slug == "st.-louis--mo--usa"
    assert p.run_date == date(2026, 7, 5)


def test_parse_rejects_unknown_provider_token():
    with pytest.raises(ValueError):
        parse_filename("bend--or_width_5000_height_5000_step_20_notaprovider_2026-07-05.csv.gz")


def test_generate_run_filename_rejects_unknown_provider():
    with pytest.raises(ValueError):
        generate_run_filename(
            "bend--or", 5000, 5000, 20, date(2026, 7, 5), provider="not-a-provider"
        )


def test_parse_archival_step_30_dated_name():
    # Archival imports (issue #93) use the predecessor scraper's 30 m step
    name = generate_run_filename("seattle--wa", 987, 1093, 30, date(2023, 11, 5))
    p = parse_filename(name + ".csv.gz")
    assert (p.width_meters, p.height_meters, p.step_meters) == (987, 1093, 30)
    assert p.run_date == date(2023, 11, 5)
    assert p.provider == "gsv"


def test_same_grid_geometry_ignores_date_and_provider():
    assert same_grid_geometry(
        "seattle--wa_width_5000_height_5000_step_20_2026-07-02.csv.gz",
        "seattle--wa_width_5000_height_5000_step_20_2026-04-01.csv.gz",
    )
    assert same_grid_geometry(
        "seattle--wa_width_5000_height_5000_step_20_2026-07-02.csv.gz",
        "seattle--wa_width_5000_height_5000_step_20_mapillary_2026-07-02.csv.gz",
    )
    # Legacy undated vs dated: geometry is all that matters
    assert same_grid_geometry(
        "seattle--wa_width_5000_height_5000_step_20.csv.gz",
        "seattle--wa_width_5000_height_5000_step_20_2026-07-02.csv.gz",
    )


def test_same_grid_geometry_rejects_mismatches():
    modern = "seattle--wa_width_5000_height_5000_step_20_2026-07-02.csv.gz"
    assert not same_grid_geometry(
        modern, "seattle--wa_width_1000_height_1000_step_30_2023-11-05.csv.gz"
    )
    assert not same_grid_geometry(
        modern, "seattle--wa_width_5000_height_5000_step_30_2026-07-02.csv.gz"
    )
    assert not same_grid_geometry(
        modern, "seattle--wa_width_4000_height_5000_step_20_2026-07-02.csv.gz"
    )
    assert not same_grid_geometry(
        modern, "seattle--wa_width_5000_height_4000_step_20_2026-07-02.csv.gz"
    )


def test_same_grid_geometry_unparseable_is_false():
    modern = "seattle--wa_width_5000_height_5000_step_20_2026-07-02.csv.gz"
    assert not same_grid_geometry(modern, "cities.json.gz")
    assert not same_grid_geometry("garbage", "garbage")


def test_sanitize_city_query_str():
    # Interior periods are preserved — matches all legacy data-file slugs
    assert sanitize_city_query_str("St. Louis, MO, USA") == "st.-louis--mo--usa"
    assert sanitize_city_query_str("Grand Marais") == "grand-marais"
    assert sanitize_city_query_str("Port Angeles, WA") == "port-angeles--wa"
    # Nominatim sometimes returns non-breaking spaces in place names
    assert (
        sanitize_city_query_str("Ann\xa0Arbor Charter Township, Michigan")
        == "ann-arbor-charter-township--michigan"
    )


# ── Street-coverage artifacts (issues #24/#103) ─────────────────────────────


def test_streets_filename_for_run():
    assert (
        streets_filename_for_run("bend--or_width_5000_height_5000_step_20_2026-07-08.csv.gz")
        == "bend--or_width_5000_height_5000_step_20_2026-07-08_streets.json.gz"
    )
    # Provider-tagged run names keep their token in the derived artifact.
    assert (
        streets_filename_for_run(
            "bend--or_width_5000_height_5000_step_20_mapillary_2026-07-08.csv.gz"
        )
        == "bend--or_width_5000_height_5000_step_20_mapillary_2026-07-08_streets.json.gz"
    )


def test_streets_filename_for_run_rejects_non_run_names():
    with pytest.raises(ValueError):
        streets_filename_for_run("bend--or_width_5000_height_5000_step_20_2026-07-08.json.gz")
    with pytest.raises(ValueError):
        streets_filename_for_run("bend--or.csv")


def test_parse_filename_rejects_streets_artifacts():
    """Streets artifacts must never parse as run files — same rejection
    contract history files rely on (a ValueError means "not a run file")."""
    with pytest.raises(ValueError):
        parse_filename("bend--or_width_5000_height_5000_step_20_2026-07-08_streets.json.gz")
    # Artifact derived from a legacy UNDATED run name: '_streets' lands where a
    # provider token would, and must be rejected, not misparsed as a provider.
    with pytest.raises(ValueError):
        parse_filename("bend--or_width_5000_height_5000_step_20_streets.json.gz")


# ── Road-walk collection artifacts (issue #99) ──────────────────────────────


def test_streetwalk_filename_round_trips():
    stem = generate_streetwalk_filename("bend--or", 5000, 5000, 20, 15, date(2026, 7, 8))
    assert stem == "bend--or_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08"
    p = parse_streetwalk_filename(stem + ".csv.gz")
    assert (p.width_meters, p.step_meters, p.spacing_meters) == (5000, 20, 15)
    assert p.run_date == date(2026, 7, 8)
    assert p.slug == "bend--or"
    # No token means GSV, exactly as for run filenames — every walk published
    # before the token existed keeps parsing as the provider it actually was.
    assert p.provider == "gsv"
    # Same for the network: tokenless is 'drive', so pre-existing walk names
    # (which have no network token at all) stay byte-identical and keep parsing.
    assert p.network_type == "drive"


def test_streetwalk_filenames_differ_per_network_type():
    """
    A 'drive' walk and an 'all_public' walk of one city cover different edge
    sets and can be collected the same night. Without a network token their
    filenames are identical, so the second collection would find the first's
    snapshot on disk and skip as a silent no-op — the same failure the provider
    token exists to prevent.
    """
    args = ("bend--or", 5000, 5000, 20, 15, date(2026, 7, 8))
    drive = generate_streetwalk_filename(*args)
    broad = generate_streetwalk_filename(*args, network_type="all_public")
    assert drive != broad
    assert broad == "bend--or_width_5000_height_5000_step_20_streetwalk_allpublic_sp15_2026-07-08"
    assert streetwalk_coverage_filename(drive + ".csv.gz") != streetwalk_coverage_filename(
        broad + ".csv.gz"
    )
    p = parse_streetwalk_filename(broad + ".csv.gz")
    assert p.network_type == "all_public"
    assert (p.provider, p.slug, p.spacing_meters) == ("gsv", "bend--or", 15)

    with pytest.raises(ValueError):
        generate_streetwalk_filename(*args, network_type="bogus")


@pytest.mark.parametrize(
    "network_type", ["drive", "all_public", "all", "walk", "bike", "drive_service"]
)
@pytest.mark.parametrize("provider", ["gsv", "mapillary"])
def test_streetwalk_filename_round_trips_every_provider_and_network(provider, network_type):
    """Every (provider, network) pair must generate a name that parses back to it.

    Note 'all' vs 'allpublic': the token alternation is matched longest-first so
    the shorter one cannot shadow the longer.
    """
    stem = generate_streetwalk_filename(
        "bend--or",
        5000,
        5000,
        20,
        15,
        date(2026, 7, 8),
        provider=provider,
        network_type=network_type,
    )
    p = parse_streetwalk_filename(stem + ".csv.gz")
    assert (p.provider, p.network_type) == (provider, network_type)
    assert (p.slug, p.step_meters, p.spacing_meters) == ("bend--or", 20, 15)
    # A walk name must never read as a grid run, whatever tokens it carries.
    with pytest.raises(ValueError):
        parse_filename(stem + ".csv.gz")


def test_streetwalk_filenames_differ_per_provider():
    """
    The two providers walk the SAME sample points and the scheduler collects
    both on the same night with the same run_date, so the provider token is the
    only thing keeping their artifacts apart. Without it the second collection
    finds the first's snapshot on disk and skips as a no-op.
    """
    args = ("bend--or", 5000, 5000, 20, 15, date(2026, 7, 8))
    gsv = generate_streetwalk_filename(*args)
    mly = generate_streetwalk_filename(*args, provider="mapillary")
    assert gsv != mly
    assert mly == "bend--or_width_5000_height_5000_step_20_mapillary_streetwalk_sp15_2026-07-08"
    assert streetwalk_coverage_filename(gsv + ".csv.gz") != streetwalk_coverage_filename(
        mly + ".csv.gz"
    )

    p = parse_streetwalk_filename(mly + ".csv.gz")
    assert p.provider == "mapillary"
    assert (p.slug, p.step_meters, p.spacing_meters) == ("bend--or", 20, 15)
    assert p.run_date == date(2026, 7, 8)

    with pytest.raises(ValueError):
        generate_streetwalk_filename(*args, provider="bogus")


def test_parse_filename_rejects_provider_tagged_streetwalk_artifacts():
    """A provider-tokened walk must still not read as a grid run of that provider."""
    with pytest.raises(ValueError):
        parse_filename(
            "bend--or_width_5000_height_5000_step_20_mapillary_streetwalk_sp15_2026-07-08.csv.gz"
        )


def test_streetwalk_coverage_filename():
    csv_name = "bend--or_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08.csv.gz"
    assert (
        streetwalk_coverage_filename(csv_name)
        == "bend--or_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08_coverage.json.gz"
    )
    with pytest.raises(ValueError):
        streetwalk_coverage_filename(
            "bend--or_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08.json.gz"
        )


def test_parse_filename_rejects_streetwalk_artifacts():
    """Streetwalk snapshots/coverage must never parse as grid run files."""
    with pytest.raises(ValueError):
        parse_filename("bend--or_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08.csv.gz")
    with pytest.raises(ValueError):
        parse_filename(
            "bend--or_width_5000_height_5000_step_20_streetwalk_sp15_2026-07-08_coverage.json.gz"
        )


def test_parse_streetwalk_rejects_normal_run_files():
    with pytest.raises(ValueError):
        parse_streetwalk_filename("bend--or_width_5000_height_5000_step_20_2026-07-08.csv.gz")


# ── Road-walk diff artifacts (issue #101) ───────────────────────────────────


def test_streetwalk_diff_filename_tokens():
    """gsv and 'drive' are tokenless; any other provider/network must be
    tokenized or a same-date-pair diff of a second series would collide —
    the exact failure mode the streetwalk provider token exists for."""
    assert (
        generate_streetwalk_diff_filename("bend--or", "2026-07-08", "2026-10-01")
        == "bend--or_streetwalkdiff_2026-07-08_to_2026-10-01.csv.gz"
    )
    assert (
        generate_streetwalk_diff_filename(
            "bend--or", "2026-07-08", "2026-10-01", provider="mapillary"
        )
        == "bend--or_streetwalkdiff_mapillary_2026-07-08_to_2026-10-01.csv.gz"
    )
    assert (
        generate_streetwalk_diff_filename(
            "bend--or", "2026-07-08", "2026-10-01", network_type="all_public"
        )
        == "bend--or_streetwalkdiff_allpublic_2026-07-08_to_2026-10-01.csv.gz"
    )
    assert (
        generate_streetwalk_diff_filename(
            "bend--or", "2026-07-08", "2026-10-01", provider="mapillary", network_type="all_public"
        )
        == "bend--or_streetwalkdiff_mapillary_allpublic_2026-07-08_to_2026-10-01.csv.gz"
    )


def test_streetwalk_diff_filename_rejects_unknown_tokens():
    with pytest.raises(ValueError):
        generate_streetwalk_diff_filename("bend--or", "2026-07-08", "2026-10-01", provider="bing")
    with pytest.raises(ValueError):
        generate_streetwalk_diff_filename(
            "bend--or", "2026-07-08", "2026-10-01", network_type="all-public"
        )


def test_parse_filename_rejects_streetwalk_diff_artifacts():
    """Walk-diff detail files must never parse as run or streetwalk files —
    the same ValueError-means-not-mine contract every artifact family keeps."""
    names = [
        generate_streetwalk_diff_filename("bend--or", "2026-07-08", "2026-10-01"),
        generate_streetwalk_diff_filename(
            "bend--or", "2026-07-08", "2026-10-01", provider="mapillary"
        ),
        generate_streetwalk_diff_filename(
            "bend--or", "2026-07-08", "2026-10-01", network_type="all_public"
        ),
        generate_streetwalk_diff_filename(
            "bend--or", "2026-07-08", "2026-10-01", provider="mapillary", network_type="all_public"
        ),
    ]
    for name in names:
        with pytest.raises(ValueError):
            parse_filename(name)
        with pytest.raises(ValueError):
            parse_streetwalk_filename(name)
