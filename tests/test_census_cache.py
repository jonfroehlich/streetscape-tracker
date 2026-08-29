"""
The shared census cache seam (issue #290).

A completed checkpoint is PROMOTED into `census_cache/<provider>/<city>_<bbox>`
and every later consumer of that (provider, city, bbox) observation reads it for
zero requests. The provider-side halves — reassembly, the request counters, what
a reuser inherits — are pinned in `test_mapillary_resume.py` and
`test_kartaview_collector.py`; what is pinned HERE is the plumbing they both
stand on, and it is mostly about two things:

  * WHAT THE KEY IS AND WHAT THE RECORD IS. A checkpoint path carries the
    channel and the variant so two crawls can never resume each other's spend; a
    cache path carries NEITHER, because the census content depends on geometry
    and nothing else, and who paid is written INTO the entry. Getting that
    backwards would be silent — everything would still work, and the reuse the
    feature exists for would simply never happen.
  * THE COMMIT POINT. The marker is written AFTER the rename, so a crash between
    them leaves a marker-less directory that every loader deletes. Written the
    other way round it would leave a marker describing a directory that does not
    hold what it claims, which is a wrong artifact rather than a re-fetch.
"""

import errno
import json
import os
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from streetscape_metadata_tracker import checkpointing as cp

BBOX = (-121.3011234, 44.0491, -121.2988766, 44.0509)
CITY = "bend--oregon--united-states"


def _marker(**overrides):
    marker = {
        "format_version": cp.CENSUS_CACHE_FORMAT_VERSION,
        "provider": "mapillary",
        "fetched_by": "mapillary",
        "fetched_variant": None,
        "crawl_started_at": datetime.now(UTC).isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "api_requests_total": 12,
        "failed": [],
    }
    marker.update(overrides)
    return marker


def _checkpoint(tmp_path, name="ckpt", *, contents=b"census bytes"):
    """A stand-in for a completed checkpoint directory: a file to carry across."""
    path = tmp_path / name
    path.mkdir()
    (path / "state.json").write_bytes(contents)
    return str(path)


def _promoted(tmp_path, *, provider="mapillary", city=CITY, bbox=BBOX, **marker_overrides):
    """Promote a stand-in checkpoint and return the cache path."""
    cache_path = cp.census_cache_path_for(provider, city, bbox)
    assert cp.promote_checkpoint_to_cache(
        _checkpoint(tmp_path, name=f"ckpt-{city}-{provider}"),
        cache_path,
        _marker(**marker_overrides),
    )
    return cache_path


# ── The key: geometry only. Who paid is RECORDED, never keyed ──────────────


def test_the_cache_path_is_keyed_on_provider_city_and_bbox_and_nothing_else():
    """
    The whole feature in one assertion. `checkpoint_path_for` deliberately keys
    the channel and the variant so two crawls cannot resume each other's spend;
    if this path did the same, the grid run and the walk would never meet and
    every census would still be bought twice — silently, with nothing failing.
    """
    grid_style = cp.census_cache_path_for("mapillary", CITY, BBOX)
    walk_style = cp.census_cache_path_for("mapillary", CITY, BBOX)
    assert grid_style == walk_style

    # The channel and the variant that DO separate two checkpoints must leave no
    # trace here, or a walk would look for an entry the grid run never wrote.
    leaf = os.path.basename(grid_style)
    assert "mapillary_streets" not in grid_style.removeprefix(cp.census_cache_dir())
    assert "drive" not in leaf and "all_public" not in leaf
    assert CITY in leaf


def test_a_different_provider_gets_a_different_entry():
    """Two providers share a bbox and nothing else — not even a part schema."""
    assert cp.census_cache_path_for("mapillary", CITY, BBOX) != cp.census_cache_path_for(
        "kartaview", CITY, BBOX
    )


def test_a_resized_grid_gets_a_different_entry():
    """
    The frozen grid CAN be re-registered (scripts/resize_city.py,
    cap_oversized_grids.py). An entry keyed on the slug alone would survive that
    and be reused onto a lattice it does not describe — so the bbox is folded in,
    at the same 6 dp a checkpoint path uses.
    """
    resized = (BBOX[0], BBOX[1], BBOX[2] + 0.01, BBOX[3])
    assert cp.census_cache_path_for("mapillary", CITY, BBOX) != cp.census_cache_path_for(
        "mapillary", CITY, resized
    )
    # 6 dp is ~0.1 m: a difference below it is the same lattice, and rounding
    # them apart would mean a re-fetch every night for no reason.
    imperceptible = (BBOX[0] + 1e-9, BBOX[1], BBOX[2], BBOX[3])
    assert cp.census_cache_path_for("mapillary", CITY, BBOX) == cp.census_cache_path_for(
        "mapillary", CITY, imperceptible
    )


def test_the_path_carries_no_date():
    """
    A census is reused ACROSS the gap between two channels' collections, which a
    budget deferral can stretch to a night or more. A date in the key would make
    every such reuse miss — the one case the cache exists for.
    """
    leaf = os.path.basename(cp.census_cache_path_for("mapillary", CITY, BBOX))
    assert str(datetime.now(UTC).year) not in leaf


def test_the_env_override_is_realpathed(tmp_path, monkeypatch):
    """
    Same argument as checkpoint_dir(): the deployed path is reached through a
    symlink, and two spellings of one directory would be two caches, so every
    reuse would miss and the feature would silently do nothing.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv(cp.CENSUS_CACHE_DIR_ENV, str(link))
    assert cp.census_cache_dir() == os.path.realpath(str(real))


def test_the_cache_is_never_under_data(monkeypatch):
    """`data/` is rsynced to a public web server; a raw provider census is not
    ours to republish, and it would also be thousands of files of bulk data."""
    monkeypatch.delenv(cp.CENSUS_CACHE_DIR_ENV, raising=False)
    assert not cp.census_cache_dir().rstrip("/").endswith("/data")
    assert "/data/" not in cp.census_cache_dir() + "/"


def test_the_reuse_window_is_the_checkpoint_window():
    """
    ONE constant, not two that happen to agree. Both answer "how far apart may
    two halves of one dated observation be?", so splitting them would let the
    justification drift from the number.
    """
    assert cp.CENSUS_REUSE_MAX_AGE_S == cp.CHECKPOINT_MAX_AGE_S


# ── Promotion: the rename is the move, the marker is the commit ────────────


def test_promotion_moves_the_directory_and_stamps_it(tmp_path):
    checkpoint = _checkpoint(tmp_path)
    cache_path = cp.census_cache_path_for("mapillary", CITY, BBOX)

    assert cp.promote_checkpoint_to_cache(checkpoint, cache_path, _marker()) is True

    assert not os.path.exists(checkpoint), "the checkpoint MOVED; it is not a copy"
    assert open(os.path.join(cache_path, "state.json"), "rb").read() == b"census bytes", (
        "its contents came across intact"
    )
    with open(os.path.join(cache_path, cp.CENSUS_CACHE_MARKER), encoding="utf-8") as f:
        assert json.load(f)["fetched_by"] == "mapillary"


def test_the_marker_is_written_after_the_rename(tmp_path, monkeypatch):
    """
    THE COMMIT POINT. A crash between the two must leave a marker-LESS directory
    (every loader deletes one, so the cost is a re-fetch), never a marker
    describing a directory that does not hold what it claims.

    Asserted by ordering the syscalls rather than by describing them: the marker
    write is made to explode, and what must survive is the moved directory with
    no marker in it.
    """
    checkpoint = _checkpoint(tmp_path)
    cache_path = cp.census_cache_path_for("mapillary", CITY, BBOX)
    real_open = open

    def explode_on_the_marker(path, *a, **k):
        if str(path).endswith(f"{cp.CENSUS_CACHE_MARKER}.tmp"):
            raise OSError(errno.EIO, "marker write failed")
        return real_open(path, *a, **k)

    with monkeypatch.context() as patched:
        # Scoped, not undone globally: `monkeypatch` is one function-scoped
        # instance shared with conftest's autouse fixtures, so a bare undo()
        # would also drop the cache-dir isolation and point the rest of this
        # test at the working tree.
        patched.setattr("builtins.open", explode_on_the_marker)
        assert cp.promote_checkpoint_to_cache(checkpoint, cache_path, _marker()) is False

    assert os.path.isdir(cache_path), "the rename had already happened"
    assert not os.path.exists(os.path.join(cache_path, cp.CENSUS_CACHE_MARKER))
    # And that half-promoted state is not readable as a census.
    assert cp.load_census_cache_marker(cache_path) is None
    assert not os.path.exists(cache_path), "an unstamped entry is deleted, not left to rot"


def test_a_failed_promotion_leaves_the_checkpoint_where_it_was(tmp_path, monkeypatch):
    """
    Best effort in the direction that matters: a city must never fail over its
    own optimization, and the caller's fallback (keep the checkpoint, discard it
    as before) only works if the directory is still there.
    """
    checkpoint = _checkpoint(tmp_path)
    cache_path = cp.census_cache_path_for("mapillary", CITY, BBOX)

    def refuse(*a, **k):
        raise OSError(errno.EACCES, "permission denied")

    with monkeypatch.context() as patched:
        patched.setattr(cp.os, "replace", refuse)
        assert cp.promote_checkpoint_to_cache(checkpoint, cache_path, _marker()) is False

    assert os.path.exists(os.path.join(checkpoint, "state.json"))


def test_promotion_across_filesystems_falls_back_to_a_copy(tmp_path, monkeypatch):
    """
    checkpoints/ and census_cache/ are siblings by default, but either can be
    pointed elsewhere by its env override — and a rename across filesystems is
    EXDEV, not a permission problem, so it must not be reported as a failure.
    """
    checkpoint = _checkpoint(tmp_path)
    cache_path = cp.census_cache_path_for("mapillary", CITY, BBOX)
    real_replace = cp.os.replace

    def exdev_on_the_directory(src, dst, *a, **k):
        if os.path.isdir(src):
            raise OSError(errno.EXDEV, "invalid cross-device link")
        return real_replace(src, dst, *a, **k)

    with monkeypatch.context() as patched:
        patched.setattr(cp.os, "replace", exdev_on_the_directory)
        assert cp.promote_checkpoint_to_cache(checkpoint, cache_path, _marker()) is True

    assert not os.path.exists(checkpoint), "copytree + rmtree is os.replace's semantics"
    assert cp.load_census_cache_marker(cache_path) is not None


def test_promotion_replaces_a_stale_entry_wholesale(tmp_path):
    """
    Not merged. The old entry describes an EARLIER observation, so half of each
    would be a census of no single moment — and the parts are index- or
    tile-named, so a merge would silently mix them.
    """
    cache_path = _promoted(tmp_path, api_requests_total=1)
    (cache_path_extra := os.path.join(cache_path, "leftover.parquet"))
    open(cache_path_extra, "wb").close()

    second = _checkpoint(tmp_path, name="second", contents=b"newer census")
    assert cp.promote_checkpoint_to_cache(second, cache_path, _marker(api_requests_total=99))

    assert not os.path.exists(cache_path_extra), "nothing of the old entry survives"
    assert open(os.path.join(cache_path, "state.json"), "rb").read() == b"newer census"
    assert cp.load_census_cache_marker(cache_path)["api_requests_total"] == 99


# ── Loading: never raises, and deletes what it refuses ─────────────────────


def test_a_missing_entry_is_a_quiet_miss(tmp_path, caplog):
    """The ordinary first-consumer case. Not a warning: it is most reads."""
    with caplog.at_level("WARNING"):
        assert cp.load_census_cache_marker(str(tmp_path / "nothing-here")) is None
    assert caplog.text == ""


def test_a_marker_less_directory_is_deleted(tmp_path, caplog):
    """
    The crash-between-rename-and-stamp state. It may well hold a complete
    census, but nothing says who paid or when the provider was observed, so it
    cannot be reused — and leaving it would make every later consumer pay the
    same read to reach the same verdict.
    """
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    (orphan / "state.json").write_text("{}")

    with caplog.at_level("WARNING"):
        assert cp.load_census_cache_marker(str(orphan)) is None
    assert "no marker" in caplog.text
    assert not orphan.exists()


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"format_version": 99}, "marker format v99"),
        (
            {"crawl_started_at": (datetime.now(UTC) - timedelta(days=8)).isoformat()},
            "past the 7-day reuse window",
        ),
        ({"crawl_started_at": "not a timestamp", "completed_at": None}, "ValueError"),
    ],
)
def test_an_unusable_marker_is_deleted_with_a_reason(tmp_path, caplog, overrides, expected):
    cache_path = _promoted(tmp_path, **overrides)
    with caplog.at_level("WARNING"):
        assert cp.load_census_cache_marker(cache_path) is None
    assert expected in caplog.text
    assert not os.path.exists(cache_path), "an entry that will never validate has to go"


def test_a_corrupt_marker_degrades_rather_than_raising(tmp_path):
    """The loaders' never-raise posture: an unreadable entry costs a re-fetch,
    never a city."""
    cache_path = _promoted(tmp_path)
    with open(os.path.join(cache_path, cp.CENSUS_CACHE_MARKER), "w") as f:
        f.write("{not json")
    assert cp.load_census_cache_marker(cache_path) is None


def test_the_window_is_measured_from_the_crawl_start_not_the_promotion(tmp_path):
    """
    A multi-night crawl's LAST commit says nothing about how old its oldest rows
    are, so ageing from `completed_at` would let a census whose first tiles were
    fetched a fortnight ago be spliced into a snapshot dated today — the one way
    this feature could produce a wrong artifact rather than wasted work.
    """
    stale_crawl_fresh_promotion = _promoted(
        tmp_path,
        crawl_started_at=(datetime.now(UTC) - timedelta(days=10)).isoformat(),
        completed_at=datetime.now(UTC).isoformat(),
    )
    assert cp.load_census_cache_marker(stale_crawl_fresh_promotion) is None


def test_completed_at_is_the_fallback_when_a_crawl_never_checkpointed(tmp_path):
    """A crawl with no first commit to point at still has to be datable, or it
    could never be reused at all."""
    cache_path = _promoted(
        tmp_path,
        crawl_started_at=None,
        completed_at=(datetime.now(UTC) - timedelta(hours=6)).isoformat(),
    )
    assert cp.load_census_cache_marker(cache_path) is not None


def test_an_entry_from_last_night_is_still_reusable(tmp_path):
    """
    The bound's other side: this is a staleness guard, not an expiry clock. The
    age is ABSOLUTE (18 h — the grid run last night, the walk deferred for
    budget to tonight) rather than derived from the constant, or shrinking the
    constant would move the fixture with it and the test would keep passing on a
    window tight enough to defeat the whole feature.
    """
    cache_path = _promoted(
        tmp_path, crawl_started_at=(datetime.now(UTC) - timedelta(hours=18)).isoformat()
    )
    assert cp.load_census_cache_marker(cache_path) is not None


# ── The probe: cheap enough to run for every due city, every night ─────────


def test_the_probe_finds_what_promotion_wrote(tmp_path):
    _promoted(tmp_path, fetched_by="mapillary", api_requests_total=31)
    marker = cp.census_cache_probe("mapillary", CITY, BBOX)
    assert marker["fetched_by"] == "mapillary"
    assert marker["api_requests_total"] == 31
    assert cp.census_cache_probe("kartaview", CITY, BBOX) is None


def test_the_probe_deletes_nothing_it_refuses(tmp_path):
    """
    A planning caller holds no host lock and runs concurrently with the children
    promoting into these entries — the scheduler prices a channel from a lane
    worker while a sibling child is mid-collection. Since promotion is a rename
    followed by a SEPARATE marker write, a probe that removed what it refused
    could delete a census a child had just renamed and not yet stamped. Deleting
    is the consumer's job, under the lock (`load_census_cache_marker`), and the
    scheduler's tail prune's, after the city loop has returned.
    """
    expired = _promoted(
        tmp_path, crawl_started_at=(datetime.now(UTC) - timedelta(days=9)).isoformat()
    )
    assert cp.census_cache_probe("mapillary", CITY, BBOX) is None
    assert os.path.isdir(expired), "the probe refused it; it must not have removed it"

    # And the mid-promotion shape it actually protects: renamed, not yet stamped.
    unstamped = cp.census_cache_path_for("kartaview", CITY, BBOX)
    os.makedirs(unstamped)
    assert cp.census_cache_probe("kartaview", CITY, BBOX) is None
    assert os.path.isdir(unstamped)

    # The consumer, which does hold the lock, is the one that clears them.
    assert cp.load_census_cache_marker(expired) is None
    assert not os.path.exists(expired)


def test_the_probe_reads_no_part_files(tmp_path, monkeypatch):
    """
    Its callers are the ones that must not pay to find out — `--estimate`, the
    walk's budget pre-flight, and `_channel_estimate`, which prices every
    channel of every due city on every night. A probe that opened the parquet
    footers would turn a planning pass into a disk sweep.
    """
    cache_path = _promoted(tmp_path)
    for i in range(3):
        (open(os.path.join(cache_path, f"tile-{i}-0.parquet"), "wb")).close()

    opened = []
    real_open = open

    def record(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    with monkeypatch.context() as patched:
        patched.setattr("builtins.open", record)
        assert cp.census_cache_probe("mapillary", CITY, BBOX) is not None
    assert all(not p.endswith(".parquet") for p in opened), opened


# ── The prune: the only thing bounding the cache's size ────────────────────


def test_prune_removes_expired_entries_and_keeps_fresh_ones(tmp_path):
    fresh = _promoted(tmp_path, city="fresh--city")
    expired = _promoted(
        tmp_path,
        city="expired--city",
        crawl_started_at=(datetime.now(UTC) - timedelta(days=9)).isoformat(),
    )
    unstamped = os.path.join(cp.census_cache_dir(), "mapillary", "unstamped--city")
    os.makedirs(unstamped)

    assert cp.prune_census_cache() == 2

    assert os.path.exists(fresh)
    assert not os.path.exists(expired)
    assert not os.path.exists(unstamped)


def test_prune_never_touches_the_checkpoint_directory(tmp_path, monkeypatch):
    """
    Different directories, different lifetimes: an in-flight checkpoint is a
    night's paid-for progress and is NOT expired by this window (its own loader
    ages it separately). Sweeping it here would delete work in flight.
    """
    live_checkpoint = cp.checkpoint_path_for(CITY, BBOX, "mapillary")
    os.makedirs(live_checkpoint)
    _promoted(tmp_path, crawl_started_at=(datetime.now(UTC) - timedelta(days=9)).isoformat())

    cp.prune_census_cache()
    assert os.path.isdir(live_checkpoint)


def test_prune_is_zero_on_a_host_that_has_never_cached_anything(tmp_path, monkeypatch):
    """It runs in the scheduler tail beside the backup and the publish, where a
    raise would cost the night's visibility (#167)."""
    monkeypatch.setenv(cp.CENSUS_CACHE_DIR_ENV, str(tmp_path / "never-created"))
    assert cp.prune_census_cache() == 0


def test_prune_survives_an_unreadable_provider_directory(tmp_path, monkeypatch):
    """Same posture: skip what cannot be read, report what was removed."""
    _promoted(tmp_path)
    real_listdir = os.listdir

    def refuse_the_provider_dir(path, *a, **k):
        if os.path.basename(str(path)) == "mapillary":
            raise PermissionError("nope")
        return real_listdir(path, *a, **k)

    with monkeypatch.context() as patched:
        patched.setattr(cp.os, "listdir", refuse_the_provider_dir)
        assert cp.prune_census_cache() == 0
    assert cp.load_census_cache_marker(cp.census_cache_path_for("mapillary", CITY, BBOX))


def test_prune_ignores_stray_files_beside_the_entries(tmp_path):
    """A directory of directories; anything else is not ours to interpret."""
    _promoted(tmp_path)
    stray = os.path.join(cp.census_cache_dir(), "mapillary", "README")
    with open(stray, "w") as f:
        f.write("hello")
    assert cp.prune_census_cache() == 0
    assert os.path.exists(stray)


# ── The isolation the rest of the suite depends on ────────────────────────


def test_the_suite_never_writes_into_the_working_tree(tmp_path):
    """
    conftest's autouse `_isolate_census_cache` is load-bearing in a way the
    checkpoint one is not: a checkpoint is deleted by its caller once the
    artifact lands, while a completed census is deliberately LEFT for the next
    consumer. Without the fixture every completed census in the suite would be
    deposited in the checkout — and, worse, handed to the next test using the
    same fixture city, whose "N requests" assertion would silently see 0.
    """
    assert cp.census_cache_dir().startswith(os.path.realpath(str(tmp_path)))
    shutil.rmtree(cp.census_cache_dir(), ignore_errors=True)
