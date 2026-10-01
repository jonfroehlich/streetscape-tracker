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


@pytest.fixture
def preview(monkeypatch, tmp_path):
    """
    Run the real ``cli._check_boundary`` with its map and browser stubbed.
    Returns a callable (conn, **args) -> (exit_code, previewed (lat, lng)).
    """
    drawn = []

    class FakeMap:
        def save(self, path):
            pass

    def fake_display(city, lat, lng, width, height, step):
        drawn.append((lat, lng))
        return FakeMap()

    monkeypatch.setattr(cli, "display_search_area", fake_display)
    monkeypatch.setattr(cli, "open_in_browser", lambda path: (True, ""))

    def run(conn, *, lat=None, lng=None, width=None, height=None, step=20):
        args = types.SimpleNamespace(
            city=QUERY, lat=lat, lng=lng, width=width, height=height, step=step
        )
        rc = cli._check_boundary(conn, args, str(tmp_path))
        return rc, (drawn[-1] if drawn else None)

    return run


def _register(conn, **kwargs):
    row, newly = resolve_or_register_city(conn, query=QUERY, **kwargs)
    # Assert on the CATALOG row, not the return value alone: the frozen row is
    # what every future run reads.
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
]


@pytest.mark.parametrize("args, expected, _source", ARG_COMBOS)
def test_the_preview_and_the_registration_choose_the_identical_center(
    conn, geocode, preview, args, expected, _source
):
    """
    The parity #186 is about: the preview runs first (it registers nothing),
    then the real registration with identical args, and both must land on
    the same point — the expected one, so a shared wrong answer fails too.
    """
    rc, previewed = preview(conn, **args)
    assert rc == 0
    assert db.resolve_city(conn, QUERY) is None, "a preview must not register"

    registered, _row, newly = _register(conn, **args)
    assert newly
    assert previewed == registered == expected


@pytest.mark.parametrize("args, expected, source", ARG_COMBOS)
def test_the_preview_prints_its_center_and_where_it_came_from(
    conn, geocode, preview, capsys, args, expected, source
):
    rc, _ = preview(conn, **args)
    assert rc == 0
    out = capsys.readouterr().out
    # The same form the real run prints after resolving geometry.
    assert f"centered at {expected[0]:.5f}, {expected[1]:.5f}" in out
    assert f"Center source: {source}" in out


def test_the_preview_without_a_bbox_prints_the_fallback_source(conn, geocode, preview, capsys):
    geocode["loc"] = _Loc(bbox_center=None)
    rc, previewed = preview(conn)
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
    rc, previewed = preview(conn, **overrides)
    assert rc == 0
    assert previewed == BBOX_MIDPOINT
    assert f"Center source: {CENTER_SOURCE_FROZEN}" in capsys.readouterr().out

    again, row, newly = _register(conn, **overrides)
    assert not newly
    assert again == BBOX_MIDPOINT
    assert (row.grid_width_m, row.grid_height_m) == AUTO_DIMS
