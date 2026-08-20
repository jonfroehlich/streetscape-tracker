"""Cross-process host lock for per-IP third parties (issue #208).

`AsyncRateLimiter` bounds ONE process. Mapillary's tile CDN and Overpass both
meter by IP, so two politely-paced processes on one machine present double the
configured rate — which is how makelab2 earned bans from both services in a
single night on 2026-08-14.

Following `tests/test_download_guard.py`: a second `FileLock` on the same path
behaves exactly like a competing process even inside one pytest process, because
`filelock` is not reentrant across distinct FileLock objects. No threads, no
subprocesses, no timing — and since the lock is taken before any request is
issued, no HTTP mocking either.

Note the autouse `_isolate_host_locks` fixture in conftest.py points the lock
directory at a per-test tmp dir, so these never touch a real collection's locks.
"""

import asyncio
import os

import pytest
from filelock import FileLock

from streetscape_metadata_tracker import host_lock as hl
from streetscape_metadata_tracker.download_common import (
    HOST_BUSY_EXIT_CODES,
    HOST_BY_BUSY_EXIT_CODE,
    HOST_BY_EXIT_CODE,
    HOST_EXIT_CODES,
    HOST_LABELS,
    HOST_MAPILLARY_TILES,
    HOST_OVERPASS,
    DownloadError,
    HostBlockedError,
    HostBusyError,
    HostUnavailableError,
    host_exit_code,
)

# --------------------------------------------------------------------------
# The lock primitive
# --------------------------------------------------------------------------


def test_a_second_holder_fails_fast_rather_than_queueing():
    other = FileLock(hl.lock_path(HOST_OVERPASS))
    other.acquire()
    try:
        with pytest.raises(HostBusyError) as excinfo:
            with hl.host_lock(HOST_OVERPASS):
                pytest.fail("acquired a lock another process holds")
    finally:
        other.release()

    assert excinfo.value.host == HOST_OVERPASS
    # A HostBusyError must remain catchable as the DownloadError the collection
    # CLIs already handle, or a busy lock would escape as an uncaught traceback.
    assert isinstance(excinfo.value, DownloadError)
    assert isinstance(excinfo.value, HostUnavailableError)


def test_the_lock_is_released_when_the_block_exits():
    with hl.host_lock(HOST_OVERPASS):
        pass
    # Would raise HostBusyError if the first hold leaked.
    with hl.host_lock(HOST_OVERPASS):
        pass


def test_the_lock_is_released_even_when_the_body_raises():
    with pytest.raises(ValueError):
        with hl.host_lock(HOST_MAPILLARY_TILES):
            raise ValueError("crawl blew up")
    with hl.host_lock(HOST_MAPILLARY_TILES):
        pass


def test_different_hosts_do_not_contend():
    """A Mapillary road walk needs Overpass AND tiles; one must not block the
    other, or the two locks would deadlock the single channel that takes both."""
    with hl.host_lock(HOST_OVERPASS):
        with hl.host_lock(HOST_MAPILLARY_TILES):
            pass


def test_the_busy_error_names_the_holder_and_says_the_file_is_not_the_lock():
    with hl.host_lock(HOST_MAPILLARY_TILES):
        # Read the owner sidecar the way a competing process would.
        owner = hl._read_owner(HOST_MAPILLARY_TILES)
    assert f"pid {os.getpid()}" in owner

    other = FileLock(hl.lock_path(HOST_MAPILLARY_TILES))
    other.acquire()
    try:
        with pytest.raises(HostBusyError) as excinfo:
            with hl.host_lock(HOST_MAPILLARY_TILES):
                pass
    finally:
        other.release()

    text = str(excinfo.value)
    assert "per-IP" in text, "must explain WHY concurrency is dangerous"
    # An operator who finds a leftover .lock file must not conclude they are
    # wedged: flock dies with the process, so the file alone means nothing.
    assert "not the file's existence" in text


def test_the_owner_file_never_fails_a_collection(monkeypatch):
    """Owner recording is diagnostics. If it can't be written, the fetch still
    runs — the alternative is a lock that breaks collection to log about it."""

    def boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(hl, "open", boom, raising=False)
    monkeypatch.setattr("builtins.open", boom)
    with hl.host_lock(HOST_OVERPASS):
        pass


# --------------------------------------------------------------------------
# Where the lock file lives — each of these guards a way it could silently
# become a no-op or a hazard.
# --------------------------------------------------------------------------


def test_the_lock_dir_is_never_under_the_published_data_dir(monkeypatch):
    """data/ is rsynced to a public web server by sync_data_to_server.sh."""
    monkeypatch.delenv(hl.LOCK_DIR_ENV, raising=False)
    from streetscape_metadata_tracker.paths import get_default_data_dir

    assert not os.path.realpath(hl.lock_dir()).startswith(
        os.path.realpath(get_default_data_dir()) + os.sep
    )


def test_the_lock_path_resolves_a_symlinked_checkout(monkeypatch, tmp_path):
    """
    Production runs from `%h/streetscape-tracker`, a symlink into
    /projects/makeabilitylab, while an operator shell uses the real path.
    `get_project_root()` uses abspath, which does NOT resolve symlinks — so
    without realpath the scheduler and the operator would take two different
    locks and neither would ever see the other.
    """
    monkeypatch.delenv(hl.LOCK_DIR_ENV, raising=False)
    real = tmp_path / "real_checkout"
    real.mkdir()
    link = tmp_path / "linked_checkout"
    link.symlink_to(real)

    monkeypatch.setattr(hl, "get_project_root", lambda: str(link))
    via_link = hl.lock_path(HOST_OVERPASS)
    monkeypatch.setattr(hl, "get_project_root", lambda: str(real))
    via_real = hl.lock_path(HOST_OVERPASS)

    assert via_link == via_real
    assert str(link) not in via_link


def test_the_lock_dir_env_override_wins(monkeypatch, tmp_path):
    """The systemd unit sets this explicitly, because PrivateTmp=true and the
    symlinked WorkingDirectory both make the derived path unreliable there."""
    monkeypatch.setenv(hl.LOCK_DIR_ENV, str(tmp_path / "elsewhere"))
    assert hl.lock_dir() == os.path.realpath(tmp_path / "elsewhere")


def test_the_override_is_resolved_the_same_way_the_default_is(monkeypatch, tmp_path):
    """
    The override must be realpath'd too, or constraint 2 comes straight back
    with no symptom: the unit sets the real path while an operator exports
    `~/streetscape-tracker/locks`, and the two processes take different locks
    while both believing they hold "the" lock.
    """
    real = tmp_path / "real_checkout"
    (real / "locks").mkdir(parents=True)
    link = tmp_path / "linked_checkout"
    link.symlink_to(real)

    monkeypatch.setenv(hl.LOCK_DIR_ENV, str(link / "locks"))
    via_link = hl.lock_path(HOST_MAPILLARY_TILES)
    monkeypatch.setenv(hl.LOCK_DIR_ENV, str(real / "locks"))
    via_real = hl.lock_path(HOST_MAPILLARY_TILES)

    assert via_link == via_real
    assert str(link) not in via_link


def test_the_override_resolves_even_before_the_lock_dir_exists(monkeypatch, tmp_path):
    """realpath resolves the existing prefix, so a first-ever run on a fresh
    checkout still agrees with every later one."""
    real = tmp_path / "real_checkout"
    real.mkdir()
    link = tmp_path / "linked_checkout"
    link.symlink_to(real)

    monkeypatch.setenv(hl.LOCK_DIR_ENV, str(link / "locks"))
    assert hl.lock_dir() == str(real / "locks")
    assert not (real / "locks").exists(), "lock_dir() must not create anything"


def test_the_lock_dir_is_read_at_call_time_not_import_time(monkeypatch, tmp_path):
    monkeypatch.setenv(hl.LOCK_DIR_ENV, str(tmp_path / "first"))
    assert hl.lock_dir().endswith("first")
    monkeypatch.setenv(hl.LOCK_DIR_ENV, str(tmp_path / "second"))
    assert hl.lock_dir().endswith("second")


# --------------------------------------------------------------------------
# The chokepoints — no request may escape while another process holds the lock
# --------------------------------------------------------------------------


def test_a_busy_lock_stops_mapillary_before_a_single_tile_is_requested(monkeypatch):
    from streetscape_metadata_tracker import download_mapillary as dm

    served = []

    async def spy(session, url, timeout, rate_limiter=None, on_request=None):
        served.append(url)
        raise AssertionError("a tile was requested while the host lock was held")

    monkeypatch.setattr(dm, "_fetch_tile", spy)

    other = FileLock(hl.lock_path(HOST_MAPILLARY_TILES))
    other.acquire()
    try:
        with pytest.raises(HostBusyError):
            asyncio.run(
                dm.fetch_city_images_async(
                    "Test City",
                    dm.grid_bbox(41.8, -87.7, 30000, 30000, 2000),
                    "MLY|test|token",
                )
            )
    finally:
        other.release()

    assert served == [], "the whole point is that zero requests reach the CDN"


def test_a_busy_lock_stops_overpass_before_the_graph_is_downloaded(monkeypatch, tmp_path):
    from streetscape_street_analyzer import download_street_network as dsn

    downloads = []

    def spy(bbox, network_type):
        downloads.append(bbox)
        raise AssertionError("Overpass was queried while the host lock was held")

    monkeypatch.setattr(dsn, "_download_graph", spy)

    other = FileLock(hl.lock_path(HOST_OVERPASS))
    other.acquire()
    try:
        with pytest.raises(HostBusyError):
            dsn.fetch_graph(_city_row(), str(tmp_path))
    finally:
        other.release()

    assert downloads == []


def test_a_cached_network_never_contends_for_the_overpass_lock(monkeypatch, tmp_path):
    """
    A warm city does zero Overpass I/O, so it must not queue behind — or fail
    because of — a process that is genuinely fetching. The lock sits after the
    cache-hit return for exactly this reason.
    """
    import networkx as nx
    import osmnx as ox

    from streetscape_street_analyzer import download_street_network as dsn

    graph = nx.MultiDiGraph()
    graph.add_edge(1, 2)
    cache = tmp_path / "osm_cache" / "bend--or_streets_network.graphml"
    cache.parent.mkdir(parents=True)
    cache.touch()
    monkeypatch.setattr(ox, "load_graphml", lambda path: graph)

    other = FileLock(hl.lock_path(HOST_OVERPASS))
    other.acquire()
    try:
        assert dsn.fetch_graph(_city_row(), str(tmp_path)) is graph
    finally:
        other.release()


def _city_row(city_id="bend--or"):
    """Same shape as tests/test_street_coverage.py's _fake_city."""
    from streetscape_metadata_tracker.db import CityRow

    return CityRow(
        city_id=city_id,
        display_name="Bend, OR",
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.31,
        grid_width_m=5000,
        grid_height_m=5000,
        step_m=20,
        created_at="2026-01-01T00:00:00+00:00",
        enabled=True,
        notes=None,
    )


# --------------------------------------------------------------------------
# Exit codes, and the deadlock guard
# --------------------------------------------------------------------------


def test_every_locked_host_has_a_distinct_exit_code():
    """The child's message never crosses the process boundary — the scheduler
    sees only returncode — so the mapping has to be total and injective."""
    # Read from HOST_LABELS -- the registry of hosts we lock -- rather than
    # from a list repeated here, so adding a fourth locked host without giving
    # it exit codes fails this test instead of silently returning 1 to the
    # scheduler and reading as an ordinary collection failure.
    for table in (HOST_EXIT_CODES, HOST_BUSY_EXIT_CODES):
        assert set(table) == set(HOST_LABELS)
        assert len(set(table.values())) == len(table)
    assert HOST_BY_EXIT_CODE == {v: k for k, v in HOST_EXIT_CODES.items()}
    assert HOST_BY_BUSY_EXIT_CODE == {v: k for k, v in HOST_BUSY_EXIT_CODES.items()}
    # Blocked and busy must never share a number: the scheduler's whole
    # reaction — night-wide breaker vs. skip one channel — keys off which
    # table the code lands in.
    assert not set(HOST_EXIT_CODES.values()) & set(HOST_BUSY_EXIT_CODES.values())
    # 0 would read as success and 1 is the generic failure every other path
    # already returns; either would make the breaker fire on ordinary bugs.
    for code in (0, 1):
        assert code not in HOST_BY_EXIT_CODE and code not in HOST_BY_BUSY_EXIT_CODE


def test_the_exit_code_distinguishes_a_busy_lock_from_a_refusal():
    """
    The two conditions have opposite lifetimes. A refusal is durable and trips
    the night-wide breaker; a busy lock ends when the other local process does,
    so escalating it would let a two-minute manual run cost the batch every
    Mapillary city of the night.
    """
    busy = HostBusyError("another process holds it", host=HOST_MAPILLARY_TILES)
    blocked = HostBlockedError("the CDN refused us", host=HOST_MAPILLARY_TILES)

    assert host_exit_code(busy) == HOST_BUSY_EXIT_CODES[HOST_MAPILLARY_TILES]
    assert host_exit_code(blocked) == HOST_EXIT_CODES[HOST_MAPILLARY_TILES]
    assert host_exit_code(busy) != host_exit_code(blocked)
    # A bare HostUnavailableError is the conservative case: treat it as a
    # refusal, since under-reacting means firing into a host already saying no.
    assert (
        host_exit_code(HostUnavailableError("unspecified", host=HOST_OVERPASS))
        == HOST_EXIT_CODES[HOST_OVERPASS]
    )


def test_the_scheduler_parent_never_takes_a_host_lock():
    """
    flock is scoped to an open file description and is NOT inherited across
    subprocess.run, so a scheduler parent holding a host lock would make EVERY
    child's timeout=0 acquire fail — turning the guard into a total outage.
    Only the collection children may hold it.
    """
    import pathlib

    import streetscape_metadata_tracker.scheduler as sched

    source = pathlib.Path(sched.__file__).read_text(encoding="utf-8")
    assert "host_lock" not in source, (
        "scheduler.py must not import or call host_lock — the parent holding an "
        "exclusive flock would deadlock every child it spawns"
    )
