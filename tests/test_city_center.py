"""
Which point a NEW city's frozen grid is centered on (issue #186).

The rule lives once, in ``city_registration.choose_center``: explicit
``--lat/--lng`` win; explicit ``--width/--height`` without a center take the
geocoder's reported point; an auto-sized grid takes the OSM bbox midpoint it was
sized from. Both the real registration (``resolve_or_register_city``) and the
preview (``cli._check_boundary``) go through it, and the parity tests below drive
BOTH real entry points rather than the helper, because a parity test whose two
sides call the same function directly proves nothing about the call sites.

Every fake location here carries three pairwise-distinct coordinate pairs — the
geocoder's point, the bbox midpoint, and the explicit --lat/--lng — so an
assertion on the frozen center can tell which of the three was chosen. Lat and
lon also differ within each pair, so a lat/lon swap cannot pass either.

No geocoding happens: both modules' bindings of ``get_city_location_data`` and
``get_search_dimensions`` are monkeypatched, and the preview's map and browser
are stubbed.
"""

import types

import pytest

from streetscape_metadata_tracker import city_registration as cr
from streetscape_metadata_tracker import cli, db
from streetscape_metadata_tracker.city_registration import (
    CENTER_SOURCE_BBOX_MIDPOINT,
    CENTER_SOURCE_EXPLICIT,
    CENTER_SOURCE_FROZEN,
    CENTER_SOURCE_GEOCODER_EXPLICIT_DIMS,
    CENTER_SOURCE_GEOCODER_NO_BBOX,
    CityResolutionError,
    resolve_or_register_city,
)

QUERY = "Goiania, Goias, Brazil"

GEOCODER_POINT = (-16.6809, -49.2533)  # the Nominatim point (downtown, in #186)
BBOX_MIDPOINT = (-16.6430, -49.2625)  # the midpoint #186 measured being frozen
EXPLICIT = (-16.7000, -49.3000)  # what an operator would pass as --lat/--lng

AUTO_DIMS = (30_000.0, 36_000.0)  # what the stubbed boundary derives (under the 40 km cap)
EXPLICIT_DIMS = (10_000.0, 12_000.0)
# register_city stores int() of each dimension, so a fractional --width is
# frozen truncated; the preview must show what will be frozen, not what was typed.
FRACTIONAL_DIMS = (2_500.5, 3_100.7)
FRACTIONAL_FROZEN = (2_500, 3_100)

# A second spelling of the same city: the catalog has no alias for it yet, but
# the (stubbed) geocode returns the same identity, so it derives QUERY's city_id.
SECOND_SPELLING = "Goiania GO"


class _Loc:
    """The subset of a geoutils location object registration reads."""

    city = "Goiania"
    state = "Goias"
    state_code = None
    country = "Brazil"
    country_code = "br"
    latitude, longitude = GEOCODER_POINT

    def __init__(self, bbox_center=BBOX_MIDPOINT):
        self.bbox_center = bbox_center


@pytest.fixture
def geocode(monkeypatch):
    """
    Stub both Nominatim seams in BOTH modules that bind them (cli imports its
    own copies for the preview). ``state["loc"]`` is what a geocode returns;
    a test sets it to None or to a bbox-less location as needed.
    """
    state = {"loc": _Loc(), "loc_calls": 0}

    def fake_loc(query, *a, **k):
        state["loc_calls"] += 1
        return state["loc"]

    def fake_dims(query, w, h):
        return AUTO_DIMS

    for module in (cr, cli):
        monkeypatch.setattr(module, "get_city_location_data", fake_loc)
        monkeypatch.setattr(module, "get_search_dimensions", fake_dims)
    return state


def _cli_args(query=QUERY, *, lat=None, lng=None, width=None, height=None, step=20):
    """The parsed-argv shape both CLI entry points read."""
    return types.SimpleNamespace(
        city=query, lat=lat, lng=lng, width=width, height=height, step=step
    )


@pytest.fixture
def preview(monkeypatch, tmp_path):
    """
    Run the real ``cli._check_boundary`` with its map and browser stubbed.
    Returns a callable (conn, query=QUERY, **args) ->
    (exit_code, previewed (lat, lng), previewed (width, height, step)).
    """
    drawn = []

    class FakeMap:
        def save(self, path):
            pass

    def fake_display(city, lat, lng, width, height, step):
        drawn.append(((lat, lng), (width, height, step)))
        return FakeMap()

    monkeypatch.setattr(cli, "display_search_area", fake_display)
    monkeypatch.setattr(cli, "open_in_browser", lambda path: (True, ""))

    def run(conn, query=QUERY, **kwargs):
        rc = cli._check_boundary(conn, _cli_args(query, **kwargs), str(tmp_path))
        center, dims = drawn[-1] if drawn else (None, None)
        return rc, center, dims

    return run


def _register(conn, query=QUERY, **kwargs):
    row, newly = resolve_or_register_city(conn, query=query, **kwargs)
    # Assert on the CATALOG row, not the return value alone: the frozen row is
    # what every future run reads.
    stored = db.resolve_city(conn, row.city_id)
    return (stored.center_lat, stored.center_lon), stored, newly


def _run_registration(conn, query=QUERY, **kwargs):
    """
    Registration through the real run's call site, ``cli._resolve_geometry``,
    with the same args object the preview receives — so the argv-to-keyword
    mapping there is pinned too (a dropped --width or swapped lat/lng in it
    would otherwise be invisible).
    """
    row, newly = cli._resolve_geometry(conn, _cli_args(query, **kwargs))
    stored = db.resolve_city(conn, row.city_id)
    return (stored.center_lat, stored.center_lon), stored, newly


def _dims(dims):
    return {"width": dims[0], "height": dims[1]}


def _center(pair):
    return {"lat": pair[0], "lng": pair[1]}


def test_fixture_coordinates_are_distinguishable():
    """Guard the guard: if two of these ever coincide, the tests below go blind."""
    pairs = {GEOCODER_POINT, BBOX_MIDPOINT, EXPLICIT}
    assert len(pairs) == 3
    assert all(lat != lng for lat, lng in pairs)


# ── The registration path ────────────────────────────────────────────────


def test_explicit_size_without_center_freezes_the_geocoder_point(conn, geocode):
    center, row, newly = _register(conn, **_dims(EXPLICIT_DIMS))
    assert newly
    assert center == GEOCODER_POINT
    assert (row.grid_width_m, row.grid_height_m) == EXPLICIT_DIMS


def test_auto_size_freezes_the_bbox_midpoint(conn, geocode):
    """Regression pin for the auto path, whose rationale #186 leaves intact."""
    center, row, _ = _register(conn)
    assert center == BBOX_MIDPOINT
    assert (row.grid_width_m, row.grid_height_m) == AUTO_DIMS


@pytest.mark.parametrize("dims", [None, EXPLICIT_DIMS], ids=["auto-size", "explicit-size"])
def test_explicit_center_wins_with_or_without_size(conn, geocode, dims):
    center, _row, _ = _register(conn, **_center(EXPLICIT), **(_dims(dims) if dims else {}))
    assert center == EXPLICIT


@pytest.mark.parametrize("dims", [None, EXPLICIT_DIMS], ids=["auto-size", "explicit-size"])
def test_a_location_without_a_bbox_falls_back_to_the_geocoder_point(conn, geocode, dims):
    geocode["loc"] = _Loc(bbox_center=None)
    center, _row, _ = _register(conn, **(_dims(dims) if dims else {}))
    assert center == GEOCODER_POINT


def test_no_geocode_result_still_registers_with_an_explicit_center_and_size(conn, geocode):
    geocode["loc"] = None
    center, row, newly = _register(conn, **_center(EXPLICIT), **_dims(EXPLICIT_DIMS))
    assert newly
    assert center == EXPLICIT
    assert (row.grid_width_m, row.grid_height_m) == EXPLICIT_DIMS


def test_no_geocode_result_and_no_center_raises(conn, geocode):
    geocode["loc"] = None
    with pytest.raises(CityResolutionError):
        resolve_or_register_city(conn, query=QUERY, **_dims(EXPLICIT_DIMS))
    assert db.resolve_city(conn, QUERY) is None


def test_registration_logs_the_center_and_its_source(conn, geocode, caplog):
    with caplog.at_level("INFO", logger=cr.__name__):
        _register(conn, **_dims(EXPLICIT_DIMS))
    assert f"{GEOCODER_POINT[0]:.5f}, {GEOCODER_POINT[1]:.5f}" in caplog.text
    assert CENTER_SOURCE_GEOCODER_EXPLICIT_DIMS in caplog.text


# ── A new city is registered DISABLED (issue #431) ───────────────────────


def _due_gsv(conn, today="2026-12-01"):
    """gsv's nightly queue on a date well after any registration here."""
    from datetime import date

    return [
        c.city_id
        for c in db.get_due_cities(
            conn,
            today=date.fromisoformat(today),
            cycle_days=90,
            grace_days=10,
            max_consecutive_failures=5,
            default_membership=True,
            provider="gsv",
        )
    ]


def test_a_new_city_is_registered_disabled_and_is_not_due(conn, geocode):
    """
    Through the collector CLI's real call site: the stored row is disabled and
    absent from gsv's queue. The second half enables it and asks the same
    query again, so the absence above is not an artifact of a query that
    could never see the city.
    """
    _, stored, newly = _run_registration(conn)
    assert newly is True
    assert stored.enabled is False
    assert stored.city_id not in _due_gsv(conn)

    db.set_city_enabled(conn, stored.city_id, True)
    assert stored.city_id in _due_gsv(conn)


def test_a_new_spelling_of_a_registered_city_keeps_its_enabled_value(conn, geocode):
    """
    An EXISTING row keeps its own `enabled` (issue #431 changes only what a NEW
    row is written with): a tracked city reached under a new spelling must not
    be dropped out of the rotation by the alias branch.
    """
    db.register_city(
        conn,
        city_name=_Loc.city,
        state_name=_Loc.state,
        state_code=_Loc.state_code,
        country_name=_Loc.country,
        country_code=_Loc.country_code,
        center_lat=BBOX_MIDPOINT[0],
        center_lon=BBOX_MIDPOINT[1],
        grid_width_m=AUTO_DIMS[0],
        grid_height_m=AUTO_DIMS[1],
        step_m=20,
        enabled=True,
    )
    assert db.resolve_city(conn, SECOND_SPELLING) is None, "no alias yet, or this tests nothing"

    _, stored, newly = _run_registration(conn, SECOND_SPELLING)

    assert newly is False
    assert stored.enabled is True


def test_registration_logs_that_the_city_is_disabled_and_how_to_enable_it(conn, geocode, caplog):
    """The log record is the operator-facing trace on the CLI and scheduler paths."""
    with caplog.at_level("INFO", logger=cr.__name__):
        _, stored, _ = _register(conn)
    assert "DISABLED" in caplog.text
    assert cr.enable_hint(stored.city_id) in caplog.text


# ── The preview, and its parity with the real registration ───────────────


ARG_COMBOS = [
    pytest.param({}, BBOX_MIDPOINT, CENTER_SOURCE_BBOX_MIDPOINT, id="auto-size"),
    pytest.param(
        _dims(EXPLICIT_DIMS),
        GEOCODER_POINT,
        CENTER_SOURCE_GEOCODER_EXPLICIT_DIMS,
        id="explicit-size",
    ),
    pytest.param(_center(EXPLICIT), EXPLICIT, CENTER_SOURCE_EXPLICIT, id="explicit-center"),
    pytest.param(
        {**_center(EXPLICIT), **_dims(EXPLICIT_DIMS)},
        EXPLICIT,
        CENTER_SOURCE_EXPLICIT,
        id="explicit-center-and-size",
    ),
    pytest.param(
        _dims(FRACTIONAL_DIMS),
        GEOCODER_POINT,
        CENTER_SOURCE_GEOCODER_EXPLICIT_DIMS,
        id="fractional-size",
    ),
]


@pytest.mark.parametrize("args, expected, _source", ARG_COMBOS)
def test_the_preview_and_the_registration_choose_the_identical_center(
    conn, geocode, preview, args, expected, _source
):
    """
    The parity #186 is about: the preview runs first (it registers nothing),
    then the real run's registration (``cli._resolve_geometry``) with the SAME
    args object, and both must land on the same point — the expected one, so a
    shared wrong answer fails too — and on the same frozen dimensions and step.
    """
    rc, previewed, previewed_dims = preview(conn, **args)
    assert rc == 0
    assert db.resolve_city(conn, QUERY) is None, "a preview must not register"

    registered, row, newly = _run_registration(conn, **args)
    assert newly
    assert previewed == registered == expected
    assert previewed_dims == (row.grid_width_m, row.grid_height_m, row.step_m)


@pytest.mark.parametrize("args, expected, source", ARG_COMBOS)
def test_the_preview_prints_its_center_and_where_it_came_from(
    conn, geocode, preview, capsys, args, expected, source
):
    rc, _, _dims_ = preview(conn, **args)
    assert rc == 0
    out = capsys.readouterr().out
    # The same form the real run prints after resolving geometry.
    assert f"centered at {expected[0]:.5f}, {expected[1]:.5f}" in out
    assert f"Center source: {source}" in out


def test_the_preview_without_a_bbox_prints_the_fallback_source(conn, geocode, preview, capsys):
    geocode["loc"] = _Loc(bbox_center=None)
    rc, previewed, _dims_ = preview(conn)
    assert rc == 0
    assert previewed == GEOCODER_POINT
    assert f"Center source: {CENTER_SOURCE_GEOCODER_NO_BBOX}" in capsys.readouterr().out


def test_a_registered_city_previews_and_resolves_its_frozen_center(conn, geocode, preview, capsys):
    """
    Frozen geometry is untouched by #186: once registered (here on the auto
    path's bbox midpoint), neither the preview nor a later resolution moves
    it, whatever center or size the caller now passes.
    """
    frozen, _row, _ = _register(conn)
    assert frozen == BBOX_MIDPOINT
    capsys.readouterr()

    overrides = {**_center(EXPLICIT), **_dims(EXPLICIT_DIMS)}
    rc, previewed, _dims_ = preview(conn, **overrides)
    assert rc == 0
    assert previewed == BBOX_MIDPOINT
    assert f"Center source: {CENTER_SOURCE_FROZEN}" in capsys.readouterr().out

    again, row, newly = _register(conn, **overrides)
    assert not newly
    assert again == BBOX_MIDPOINT
    assert (row.grid_width_m, row.grid_height_m) == AUTO_DIMS


def test_a_fractional_size_previews_the_dimensions_that_will_be_frozen(
    conn, geocode, preview, capsys
):
    rc, _, dims = preview(conn, **_dims(FRACTIONAL_DIMS))
    assert rc == 0
    assert dims == (*FRACTIONAL_FROZEN, 20)
    out = capsys.readouterr().out
    assert f"Grid: {FRACTIONAL_FROZEN[0]}m x {FRACTIONAL_FROZEN[1]}m, step 20m, centered at" in out


def test_a_new_spelling_of_a_registered_city_is_previewed_and_resolved_as_registered(
    conn, geocode, preview, capsys, caplog
):
    """
    A query with no alias yet that geocodes to an existing city_id is NOT a new
    city: register_city's INSERT OR IGNORE keeps the existing row, so the
    preview and the registration must both report its frozen geometry rather
    than a center they would never freeze (#400 review). The overrides-ignored
    warning fires, the spelling is aliased by the real registration only, and
    newly_registered is False.
    """
    frozen, first, _ = _register(conn)
    assert frozen == BBOX_MIDPOINT
    assert db.resolve_city(conn, SECOND_SPELLING) is None, "no alias yet, or this tests nothing"
    capsys.readouterr()

    with caplog.at_level("WARNING"):
        rc, previewed, dims = preview(conn, SECOND_SPELLING, **_dims(EXPLICIT_DIMS))
    assert rc == 0
    assert previewed == BBOX_MIDPOINT
    assert dims == (*AUTO_DIMS, 20)
    assert f"Center source: {CENTER_SOURCE_FROZEN}" in capsys.readouterr().out
    assert "--width/--height ignored" in caplog.text
    assert db.resolve_city(conn, SECOND_SPELLING) is None, "a preview must not alias"

    caplog.clear()
    with caplog.at_level("INFO", logger=cr.__name__):
        center, row, newly = _run_registration(conn, SECOND_SPELLING, **_dims(EXPLICIT_DIMS))
    assert not newly
    assert row.city_id == first.city_id
    assert center == BBOX_MIDPOINT
    assert (row.grid_width_m, row.grid_height_m) == AUTO_DIMS
    assert "--width/--height ignored" in caplog.text
    assert "Grid center" not in caplog.text, "logged a center that was never frozen"
    alias = db.resolve_city(conn, SECOND_SPELLING)
    assert alias is not None and alias.city_id == first.city_id
