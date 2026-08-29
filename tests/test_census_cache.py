"""
The shared census cache seam (issue #290).

A completed checkpoint is PROMOTED into `census_cache/<provider>/<city>_<bbox>`
and every later consumer of that (provider, city, bbox) observation reads it for
zero requests. The provider-side halves — reassembly, what a reuser inherits —
are pinned in `test_mapillary_resume.py` and `test_kartaview_collector.py`;
what is pinned HERE is the plumbing they both stand on, and it is about three
things:

  * WHAT THE KEY IS AND WHAT THE RECORD IS. A checkpoint path carries the
    channel and the variant so two crawls can never resume each other's spend; a
    cache path carries NEITHER, because the census content depends on geometry
    and nothing else, and who paid is written INTO the entry. Getting that
    backwards would be silent — everything would still work, and the reuse the
    feature exists for would simply never happen.
  * THE COMMIT POINT. The marker is written INSIDE the checkpoint and the single
    rename is the commit, so there is no instant at which an entry sits under
    its name unstamped. That is what lets the pruner run without a lock, and
    what lets a False return promise that the checkpoint is still where it was.
    The first version wrote the marker AFTER the rename and had both holes.
  * THE SHARED LIFECYCLE. The loader skeleton, the reuse accounting, the
    observation timestamp and the reconciliation of a hit with the consumer's
    own checkpoint live ONCE, so a rule enforced for one provider is enforced
    for the other — the first version had the two copies disagree about what
    "the same crawl" meant.
"""

import errno
import json
import os
import shutil
import time
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from streetscape_metadata_tracker import checkpointing as cp
from tests.conftest import stamp_census_cache

BBOX = (-121.3011234, 44.0491, -121.2988766, 44.0509)
CITY = "bend--oregon--united-states"


def _marker(**overrides):
    """A marker through the production builder, then overridden where a test
    needs a malformed one."""
    marker = cp.census_cache_marker(
        "mapillary",
        fetched_by="mapillary",
        fetched_variant=None,
        crawl_started_at=datetime.now(UTC).isoformat(),
        api_requests_total=12,
        failed=[],
    )
    marker.update(overrides)
    return marker


def _checkpoint(tmp_path, name="ckpt", *, contents=b"census bytes"):
    """A stand-in for a completed checkpoint directory: a file to carry across."""
    path = tmp_path / name
    path.mkdir()
    (path / "state.json").write_bytes(contents)
    return str(path)


def _committed_checkpoint(tmp_path, name, *, created_at):
    """A checkpoint with a commit record, as both providers write one."""
    path = str(tmp_path / name)
    os.makedirs(path)
    cp._write_json_durable(cp._state_path(path), {"created_at": created_at})
    return path


def _promoted(tmp_path, *, provider="mapillary", city=CITY, bbox=BBOX, **marker_overrides):
    """Promote a stand-in checkpoint and return the cache path."""
    cache_path = cp.census_cache_path_for(provider, city, bbox)
    assert cp.promote_checkpoint_to_cache(
        _checkpoint(tmp_path, name=f"ckpt-{city}-{provider}"),
        cache_path,
        _marker(**marker_overrides),
    )
    return cache_path


def _city(city_id=CITY):
    return SimpleNamespace(
        city_id=city_id,
        center_lat=44.05,
        center_lon=-121.30,
        grid_width_m=200,
        grid_height_m=200,
        step_m=20,
    )


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


def test_the_checkpoint_and_the_cache_spell_one_bbox_the_same_way():
    """
    A promoted entry is keyed on the bbox its checkpoint was keyed on. Two
    format strings that happened to agree would let one be "tidied" to 5 dp and
    every promotion land under a name no consumer derives.
    """
    checkpoint_leaf = os.path.basename(cp.checkpoint_path_for(CITY, BBOX, "mapillary"))
    cache_leaf = os.path.basename(cp.census_cache_path_for("mapillary", CITY, BBOX))
    assert checkpoint_leaf == cache_leaf


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


def test_every_census_provider_declares_its_store_format_in_one_place():
    """
    `CENSUS_PROVIDERS` is derived from the format table, so a provider cannot be
    a census provider on one surface and not the other — and each provider's
    module reads its format FROM here, so the probe compares against the same
    number the loader does.
    """
    from streetscape_metadata_tracker import download_kartaview, download_mapillary

    assert cp.CENSUS_PROVIDERS == {"kartaview", "mapillary"}
    assert (
        download_mapillary.MAPILLARY_CHECKPOINT_FORMAT_VERSION
        is cp.STORE_FORMAT_VERSIONS["mapillary"]
    )
    assert download_kartaview.CHECKPOINT_FORMAT_VERSION is cp.STORE_FORMAT_VERSIONS["kartaview"]


def test_crawl_store_for_derives_both_paths_from_one_city_row():
    """
    The grid CLI, the walk collector and the scheduler's probe used to spell the
    provider test and the bbox each in their own words; a channel missed in any
    one of them was silently priced at full cost or never reached the cache.
    """
    city = _city()
    assert cp.crawl_store_for("gsv", city, "gsv_streets") == (None, None)

    checkpoint, cache = cp.crawl_store_for(
        "mapillary",
        city,
        "mapillary_streets",
        variant="drive",
        reuse=False,
        run_date=date(2026, 7, 8),
    )
    assert "mapillary_streets" in checkpoint and checkpoint.endswith("_drive")
    assert cache.path == cp.census_cache_path_for("mapillary", CITY, cp.frozen_bbox(city))
    assert cache.reuse is False
    assert cache.run_date == date(2026, 7, 8)
    # The two are keyed on the SAME lattice: only the channel/variant differ.
    assert os.path.basename(checkpoint).removesuffix("_drive") == os.path.basename(cache.path)


# ── Promotion: the marker travels inside, the rename is the commit ─────────


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


def test_the_marker_is_inside_the_directory_before_it_is_renamed(tmp_path, monkeypatch):
    """
    THE COMMIT POINT. Asserted at the syscall: when the directory is renamed
    into place its marker is already durable inside it, so no instant exists at
    which an entry sits under its name unstamped. That is the property the
    lock-free pruner and the "False means still there" promise both rest on.
    """
    checkpoint = _checkpoint(tmp_path)
    cache_path = cp.census_cache_path_for("mapillary", CITY, BBOX)
    real_replace = cp.os.replace
    seen = []

    def observe(src, dst, *a, **k):
        if os.path.isdir(src):
            seen.append(os.path.exists(os.path.join(src, cp.CENSUS_CACHE_MARKER)))
        return real_replace(src, dst, *a, **k)

    with monkeypatch.context() as patched:
        patched.setattr(cp.os, "replace", observe)
        assert cp.promote_checkpoint_to_cache(checkpoint, cache_path, _marker()) is True

    assert seen == [True], "the directory rename found its marker already inside"
    assert cp.load_census_cache_marker(cache_path) is not None


def test_a_failed_marker_write_leaves_the_checkpoint_untouched(tmp_path, monkeypatch):
    """
    The marker is the FIRST step, so its failure (ENOSPC, EACCES on the shared
    ZFS) happens before anything has moved: the checkpoint is exactly as it
    was, with no half-written marker inside it, and nothing sits under the
    cache name.
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

    assert os.path.exists(os.path.join(checkpoint, "state.json")), "still where it was"
    assert sorted(os.listdir(checkpoint)) == ["state.json"], "and with nothing extra in it"
    assert not os.path.exists(cache_path)


def test_a_failed_rename_leaves_the_checkpoint_where_it_was(tmp_path, monkeypatch):
    """
    Best effort in the direction that matters: a city must never fail over its
    own optimization, and the caller's fallback (keep the checkpoint, discard it
    as before) only works if the directory is still there — and a stray marker
    inside it would be a lie to anyone reading the directory.
    """
    checkpoint = _checkpoint(tmp_path)
    cache_path = cp.census_cache_path_for("mapillary", CITY, BBOX)
    real_replace = cp.os.replace

    def refuse_the_directory(src, dst, *a, **k):
        if os.path.isdir(src):
            raise OSError(errno.EACCES, "permission denied")
        return real_replace(src, dst, *a, **k)

    with monkeypatch.context() as patched:
        patched.setattr(cp.os, "replace", refuse_the_directory)
        assert cp.promote_checkpoint_to_cache(checkpoint, cache_path, _marker()) is False

    assert os.path.exists(os.path.join(checkpoint, "state.json"))
    assert not os.path.exists(os.path.join(checkpoint, cp.CENSUS_CACHE_MARKER))
    assert not os.path.exists(cache_path)


def test_promotion_across_filesystems_falls_back_to_a_copy(tmp_path, monkeypatch):
    """
    checkpoints/ and census_cache/ are siblings by default, but either can be
    pointed elsewhere by its env override — and a rename across filesystems is
    EXDEV, not a permission problem, so it must not be reported as a failure.
    The copy lands under a staging name and is renamed into place in one step,
    so the entry still never appears unstamped.
    """
    checkpoint = _checkpoint(tmp_path)
    cache_path = cp.census_cache_path_for("mapillary", CITY, BBOX)
    real_replace = cp.os.replace

    def exdev_for_the_checkpoint(src, dst, *a, **k):
        if src == checkpoint:
            raise OSError(errno.EXDEV, "invalid cross-device link")
        return real_replace(src, dst, *a, **k)

    with monkeypatch.context() as patched:
        patched.setattr(cp.os, "replace", exdev_for_the_checkpoint)
        assert cp.promote_checkpoint_to_cache(checkpoint, cache_path, _marker()) is True

    assert not os.path.exists(checkpoint), "copytree + rmtree is os.replace's semantics"
    assert not os.path.exists(f"{cache_path}.tmp"), "the staging copy was renamed, not left"
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


def test_the_marker_builder_refuses_what_no_promoter_produces():
    """A promoted crawl always has a first commit to date it by, and only a
    census provider has parts to promote."""
    with pytest.raises(ValueError):
        cp.census_cache_marker(
            "mapillary",
            fetched_by="mapillary",
            fetched_variant=None,
            crawl_started_at=None,
            api_requests_total=1,
            failed=[],
        )
    with pytest.raises(ValueError):
        cp.census_cache_marker(
            "gsv",
            fetched_by="gsv",
            fetched_variant=None,
            crawl_started_at=datetime.now(UTC).isoformat(),
            api_requests_total=1,
            failed=[],
        )
    marker = _marker()
    assert marker["store_format_version"] == cp.STORE_FORMAT_VERSIONS["mapillary"]


# ── Loading: never raises, and deletes what nobody could use ───────────────


def test_a_missing_entry_is_a_quiet_miss(tmp_path, caplog):
    """The ordinary first-consumer case. Not a warning: it is most reads."""
    with caplog.at_level("WARNING"):
        assert cp.load_census_cache_marker(str(tmp_path / "nothing-here")) is None
    assert caplog.text == ""


def test_a_marker_less_directory_is_deleted(tmp_path, caplog):
    """
    Promotion is a single rename of a stamped directory, so this is never a
    promotion in flight: it is a hand-copied directory, or an entry from before
    the marker travelled with it. Nothing says who paid or when the provider
    was observed, so it cannot be reused — and leaving it would make every
    later consumer pay the same read to reach the same verdict.
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
        ({"crawl_started_at": "not a timestamp"}, "ValueError"),
        # No first commit to date it by: nothing bounds how old its rows are.
        ({"crawl_started_at": None}, "TypeError"),
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


def test_an_entry_observed_after_the_snapshot_date_is_refused_but_kept(tmp_path, caplog):
    """
    A backdated `--force --run-date` must not publish rows the provider served
    AFTER the snapshot's own date — `plausible_capture_mask` would drop the
    captures as "cannot be true" and the diff would show churn that belongs to
    a later day. The window alone cannot see this (the entry is hours old), so
    the consumer's run_date is the guard. And it is THIS consumer's problem
    only: the entry is exactly right for the consumer dated tomorrow, so it is
    refused without being deleted.
    """
    finished = datetime.now(UTC)
    cache_path = _promoted(tmp_path, completed_at=finished.isoformat())
    yesterday = finished.date() - timedelta(days=1)

    with caplog.at_level("WARNING"):
        assert cp.load_census_cache_marker(cache_path, run_date=yesterday) is None
    assert "after that date" in caplog.text
    assert os.path.isdir(cache_path), "refused for this date, kept for the others"
    assert cp.load_census_cache_marker(cache_path, run_date=finished.date()) is not None
    assert cp.load_census_cache_marker(cache_path, run_date=None) is not None


# ── The shared loader skeleton: the providers plug in two checks ───────────


def test_load_cached_store_runs_the_providers_checks_and_deletes_only_on_their_verdict(
    tmp_path,
):
    """
    One skeleton, two plug-ins. `validate` decides whether the store is intact
    for this lattice; `is_complete` is the check a resume never makes. A reason
    from either deletes the entry (nobody could use it); a
    `CacheEntryUnusableHere` from `validate` leaves it (this CALLER cannot use
    it — KartaView's page size, an explicit radius); anything raised is the
    never-raise posture's re-fetch.
    """

    def entry(city, state):
        path = cp.census_cache_path_for("mapillary", city, BBOX)
        ckpt = str(tmp_path / f"ckpt-{city}")
        os.makedirs(ckpt)
        cp._write_json_durable(cp._state_path(ckpt), state)
        assert cp.promote_checkpoint_to_cache(ckpt, path, _marker())
        return path

    def validate(state):
        return state["n"], None

    def complete(handle, state, marker):
        return None

    ok = entry("ok--city", {"n": 3})
    loaded = cp.load_cached_store(
        ok, label="thing", run_date=None, validate=validate, is_complete=complete
    )
    assert loaded[0] == 3 and loaded[1]["fetched_by"] == "mapillary"
    assert os.path.isdir(ok)

    bad_store = entry("bad--city", {"n": 3})
    assert (
        cp.load_cached_store(
            bad_store,
            label="thing",
            run_date=None,
            validate=lambda s: (None, "wrong lattice"),
            is_complete=complete,
        )
        is None
    )
    assert not os.path.exists(bad_store), "unusable by anyone: deleted"

    partial = entry("partial--city", {"n": 3})
    assert (
        cp.load_cached_store(
            partial,
            label="thing",
            run_date=None,
            validate=validate,
            is_complete=lambda h, s, m: "only a COMPLETE census is reusable",
        )
        is None
    )
    assert not os.path.exists(partial)

    def not_for_me(state):
        raise cp.CacheEntryUnusableHere("swept at ipp=200, this run uses ipp=2000")

    theirs = entry("theirs--city", {"n": 3})
    assert (
        cp.load_cached_store(
            theirs, label="thing", run_date=None, validate=not_for_me, is_complete=complete
        )
        is None
    )
    assert os.path.isdir(theirs), "unusable by THIS caller only: kept for the others"

    torn = entry("torn--city", {"n": 3})
    os.remove(cp._state_path(torn))
    assert (
        cp.load_cached_store(
            torn, label="thing", run_date=None, validate=validate, is_complete=complete
        )
        is None
    )
    assert not os.path.exists(torn), "a missing commit record is the never-raise re-fetch"


# ── What a reuser inherits: one rule for both providers ────────────────────


def test_reused_census_provenance_prices_only_the_crawl_that_paid():
    """
    `api_requests` is 0 for every reuser (the daily ledger is additive by
    (date, provider), so anything else bills one channel's spend against
    another's gate). `api_requests_total` is the crawl's cost ONLY for the
    (channel, variant) that paid it — the VARIANT included: an `all_public`
    walk re-finalizing the `drive` crawl is a reuse, and pricing it as the
    crawl was exactly what the KartaView copy of this rule got wrong.
    """
    marker = _marker(fetched_by="mapillary_streets", fetched_variant="drive", api_requests_total=41)

    own = cp.reused_census_provenance(marker, channel="mapillary_streets", variant="drive")
    assert own["api_requests"] == 0
    assert own["api_requests_total"] == 41
    assert own["checkpoint_path"] is None, "the entry is shared; nobody's to discard"
    assert own["census_reused"] is True
    assert own["census_fetched_by"] == "mapillary_streets"
    assert own["census_fetched_at"] == marker["crawl_started_at"]

    other_variant = cp.reused_census_provenance(
        marker, channel="mapillary_streets", variant="all_public"
    )
    assert other_variant["api_requests_total"] == 0
    other_channel = cp.reused_census_provenance(marker, channel="mapillary", variant=None)
    assert other_channel["api_requests_total"] == 0
    assert cp.same_crawl(marker, "mapillary_streets", "drive")
    assert not cp.same_crawl(marker, "mapillary_streets", None)


def test_observation_timestamp_is_the_crawls_only_for_a_reuse():
    """
    A reused census is stamped with when the provider was observed; a fresh one
    keeps this process's clock, which is what #256's byte-identity contract
    between an interrupted and an uninterrupted census is written against.
    """
    started = "2026-07-08T09:00:00+00:00"
    observed = "2026-07-07T03:00:00+00:00"
    assert (
        cp.observation_timestamp({"census_reused": True, "census_fetched_at": observed}, started)
        == observed
    )
    assert (
        cp.observation_timestamp({"census_reused": False, "census_fetched_at": observed}, started)
        == started
    )
    assert (
        cp.observation_timestamp({"census_reused": True, "census_fetched_at": None}, started)
        == started
    )
    assert cp.observation_timestamp({}, started) == started


# ── Reconciling a hit with the consumer's own checkpoint ───────────────────


def test_a_hit_discards_this_channels_older_checkpoint(tmp_path, caplog):
    """
    Last night's walk, host-blocked at tile 300, left a partial checkpoint; the
    grid run then completed and promoted. The entry supersedes the partial, and
    nothing else would ever touch it: a hit returns before the checkpoint is
    opened and no pruner walks checkpoints/.
    """
    older = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    checkpoint = _committed_checkpoint(tmp_path, "cp-walk", created_at=older)
    cache_path = _promoted(tmp_path, crawl_started_at=datetime.now(UTC).isoformat())

    with caplog.at_level("INFO"):
        reuse = cp.reconcile_cache_hit(
            cp.load_census_cache_marker(cache_path),
            cache_path=cache_path,
            checkpoint_path=checkpoint,
            channel="mapillary_streets",
            variant="drive",
        )
    assert reuse is True
    assert not os.path.exists(checkpoint), "superseded"
    assert "supersedes it" in caplog.text


def test_a_hit_yields_to_a_newer_checkpoint_of_this_channel(tmp_path, caplog):
    """
    An interrupted `--refetch-census` sweep, nine thousand requests in, is the
    NEWER observation and the one the operator asked for: it is resumed rather
    than abandoned for the stale entry it was started to replace.
    """
    cache_path = _promoted(
        tmp_path, crawl_started_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()
    )
    checkpoint = _committed_checkpoint(
        tmp_path, "cp-refetch", created_at=datetime.now(UTC).isoformat()
    )

    with caplog.at_level("INFO"):
        reuse = cp.reconcile_cache_hit(
            cp.load_census_cache_marker(cache_path),
            cache_path=cache_path,
            checkpoint_path=checkpoint,
            channel="mapillary",
            variant=None,
        )
    assert reuse is False
    assert os.path.exists(cp._state_path(checkpoint)), "left for the resume"
    assert os.path.isdir(cache_path), "and the entry stays until the resume replaces it"
    assert "newer observation" in caplog.text


def test_an_own_entry_with_failed_work_is_handed_back_for_re_probing(tmp_path, caplog):
    """
    The crawl's OWN channel coming back after its tail died is #239/#256's
    re-finalize, and a resume from a COMPLETE checkpoint re-probes what failed,
    because a refusal is time-varying. Reusing the entry as-is would inherit
    the holes for zero requests and silently drop that re-probe — so the entry
    is moved back to the checkpoint path, marker removed, for the ordinary
    resume path to finish.
    """
    cache_path = _promoted(tmp_path, fetched_by="kartaview", failed=[{"cell": 1}])
    checkpoint = str(tmp_path / "cp-kartaview")

    with caplog.at_level("WARNING"):
        reuse = cp.reconcile_cache_hit(
            cp.load_census_cache_marker(cache_path),
            cache_path=cache_path,
            checkpoint_path=checkpoint,
            channel="kartaview",
            variant=None,
        )
    assert reuse is False
    assert not os.path.exists(cache_path), "moved, not copied"
    assert os.path.exists(os.path.join(checkpoint, "state.json"))
    assert not os.path.exists(os.path.join(checkpoint, cp.CENSUS_CACHE_MARKER))
    assert "re-probes them" in caplog.text


def test_an_own_entry_with_nothing_failed_is_simply_reused(tmp_path):
    """The resume would produce the identical census for zero requests, so the
    reuse IS the re-finalize."""
    cache_path = _promoted(tmp_path, fetched_by="kartaview", failed=[])
    checkpoint = str(tmp_path / "cp-kartaview")

    reuse = cp.reconcile_cache_hit(
        cp.load_census_cache_marker(cache_path),
        cache_path=cache_path,
        checkpoint_path=checkpoint,
        channel="kartaview",
        variant=None,
    )
    assert reuse is True
    assert os.path.isdir(cache_path) and not os.path.exists(checkpoint)


def test_another_channels_failed_holes_are_inherited_not_handed_back(tmp_path):
    """A cross-channel reuser publishes the SAME observation; only the crawl's
    own channel has a re-probe to lose."""
    cache_path = _promoted(tmp_path, fetched_by="mapillary", failed=[[1, 2]])
    reuse = cp.reconcile_cache_hit(
        cp.load_census_cache_marker(cache_path),
        cache_path=cache_path,
        checkpoint_path=str(tmp_path / "cp-walk"),
        channel="mapillary_streets",
        variant="drive",
    )
    assert reuse is True
    assert os.path.isdir(cache_path)


def test_a_hit_with_no_checkpoint_path_is_just_a_hit(tmp_path):
    cache_path = _promoted(tmp_path)
    assert cp.reconcile_cache_hit(
        cp.load_census_cache_marker(cache_path),
        cache_path=cache_path,
        checkpoint_path=None,
        channel="mapillary",
        variant=None,
    )


# ── The probe: cheap enough to run for every due city, every night ─────────


def test_the_probe_finds_what_promotion_wrote(tmp_path):
    _promoted(tmp_path, fetched_by="mapillary", api_requests_total=31)
    marker = cp.census_cache_probe("mapillary", CITY, BBOX)
    assert marker["fetched_by"] == "mapillary"
    assert marker["api_requests_total"] == 31
    assert cp.census_cache_probe("kartaview", CITY, BBOX) is None


def test_the_probe_refuses_an_entry_whose_commit_record_this_build_cannot_read(tmp_path):
    """
    The probe prices a channel at 0 and the scheduler passes the child no
    request cap, so a hit the child's loader then refuses on format is a full
    fetch with the budget gate already passed. The marker records the store
    format it was promoted under, and the probe compares it against this
    build's — a bumped format is a MISS here, not a hit that turns into a
    fetch.
    """
    stale_format = _promoted(tmp_path, store_format_version=99)
    assert cp.census_cache_probe("mapillary", CITY, BBOX) is None
    assert os.path.isdir(stale_format), "the probe deletes nothing"


def test_the_probe_deletes_nothing_it_refuses(tmp_path):
    """
    A planning caller holds no host lock and runs concurrently with the
    consumers reading these entries. Deleting is the consumer's job, under the
    lock (`load_census_cache_marker`), and the tail prune's.
    """
    expired = _promoted(
        tmp_path, crawl_started_at=(datetime.now(UTC) - timedelta(days=9)).isoformat()
    )
    assert cp.census_cache_probe("mapillary", CITY, BBOX) is None
    assert os.path.isdir(expired), "the probe refused it; it must not have removed it"

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


def _age(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_prune_removes_expired_entries_and_settled_debris_and_keeps_the_rest(tmp_path):
    """
    Three shapes go: an entry past the window, a marker-less directory nothing
    has touched for a day, and an EXDEV staging copy (`<entry>.tmp`) likewise
    settled. Two stay: a fresh entry, and debris young enough to be a copy
    still in progress — promotion is one rename, so nothing else is ever "in
    flight" here, and that grace is the only concurrency the pruner needs.
    """
    fresh = _promoted(tmp_path, city="fresh--city")
    expired = _promoted(
        tmp_path,
        city="expired--city",
        crawl_started_at=(datetime.now(UTC) - timedelta(days=9)).isoformat(),
    )
    provider_dir = os.path.join(cp.census_cache_dir(), "mapillary")
    settled_orphan = os.path.join(provider_dir, "orphan--city")
    os.makedirs(settled_orphan)
    _age(settled_orphan, days=2)
    settled_staging = os.path.join(provider_dir, "copied--city.tmp")
    os.makedirs(settled_staging)
    _age(settled_staging, days=2)
    copying = os.path.join(provider_dir, "copying--city.tmp")
    os.makedirs(copying)

    assert cp.prune_census_cache() == 3

    assert os.path.exists(fresh)
    assert os.path.exists(copying), "a copy may still be in progress"
    for gone in (expired, settled_orphan, settled_staging):
        assert not os.path.exists(gone), gone


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


def test_the_suite_helper_and_the_promoter_write_the_same_marker(tmp_path):
    """
    `tests.conftest.stamp_census_cache` is what the scheduler and walk tests
    hand the probe. It must be an entry the promoter could have written, or
    those tests pin a reader against a shape no writer produces.
    """
    stamped = stamp_census_cache(
        cp.census_cache_path_for("kartaview", CITY, BBOX), "kartaview", fetched_by="kartaview"
    )
    assert set(cp.load_census_cache_marker(stamped)) == set(_marker())
    assert cp.census_cache_probe("kartaview", CITY, BBOX)["fetched_by"] == "kartaview"


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
