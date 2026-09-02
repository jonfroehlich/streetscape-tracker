"""What the nightly cgroup memory reading pins (issue #305).

The reading exists to answer one question — how close did the night come to
`MemoryHigh`, the cap whose breach is invisible — so the tests here are mostly
about the ways it can be *wrong quietly*: a zero that reads as headroom, a
percentage against the wrong cap, or an exception in the tail that costs the
publish.
"""

import os

import pytest

from streetscape_metadata_tracker import cgroup_memory


def _tree(tmp_path, *, relative="/user.slice/streetscape-tracker.service", **files):
    """Build a fake `/sys/fs/cgroup` + `/proc/self/cgroup` pair.

    `files` maps cgroup file name -> contents; omit one to simulate a kernel or
    hierarchy that does not provide it.
    """
    root = tmp_path / "cgroup"
    node = root / relative.lstrip("/")
    node.mkdir(parents=True)
    for name, contents in files.items():
        (node / name.replace("_", ".", 1)).write_text(contents + "\n")
    proc = tmp_path / "proc_self_cgroup"
    proc.write_text(f"0::{relative}\n")
    return str(root), str(proc)


def test_reads_peak_and_both_caps_from_the_processs_own_cgroup(tmp_path):
    """The ordinary case: v2 hierarchy, `memory.peak` present, both caps set."""
    root, proc = _tree(
        tmp_path,
        memory_peak="20267974656",  # 18.88 GiB, the 2026-09-01 prod reading
        memory_high=str(40 * 2**30),
        memory_max=str(48 * 2**30),
    )

    reading = cgroup_memory.read_cgroup_memory(root, proc)

    assert reading is not None
    assert reading.peak_bytes == 20267974656
    assert reading.high_bytes == 40 * 2**30
    assert reading.max_bytes == 48 * 2**30


def test_the_line_quotes_the_percentage_against_memory_high(tmp_path):
    """MemoryHigh, not MemoryMax, is the denominator.

    Reaching MemoryMax is an OOM kill an operator reads in a log; reaching
    MemoryHigh is a night that is merely slow (issue #157). Quoting the softer
    cap is the whole point — against MemoryMax the same 2026-09-01 night would
    have read 79% and looked like it had room.
    """
    root, proc = _tree(
        tmp_path,
        memory_peak="20267974656",
        memory_high=str(20 * 2**30),
        memory_max=str(24 * 2**30),
    )

    line = cgroup_memory.describe_cgroup_memory(root, proc)

    assert line == "cgroup peak 18.88 GiB of 20 GiB MemoryHigh (94%)"


def test_memory_max_is_the_fallback_denominator_when_the_soft_brake_is_off(tmp_path):
    """`MemoryHigh` absent (spelled `max`) is a supported configuration — the
    unit file offers "no MemoryHigh at all" as a valid answer to a big-city
    hang — so the line must still say something useful rather than silently
    dropping the percentage that makes it worth reading."""
    root, proc = _tree(
        tmp_path,
        memory_peak=str(12 * 2**30),
        memory_high="max",
        memory_max=str(48 * 2**30),
    )

    assert cgroup_memory.describe_cgroup_memory(root, proc) == (
        "cgroup peak 12.00 GiB of 48 GiB MemoryMax (25%)"
    )


def test_an_uncapped_cgroup_reports_the_peak_without_inventing_a_percentage(tmp_path):
    """A dev laptop or a manual run outside the unit has no caps at all. The
    peak is still real; a percentage would need a denominator that does not
    exist, and division by the missing cap is how this becomes an exception in
    the tail."""
    root, proc = _tree(tmp_path, memory_peak=str(3 * 2**30), memory_high="max", memory_max="max")

    assert cgroup_memory.describe_cgroup_memory(root, proc) == (
        "cgroup peak 3.00 GiB (no cgroup memory cap)"
    )


def test_a_missing_memory_peak_is_none_not_zero(tmp_path):
    """`memory.peak` arrived in kernel 5.19, and this must degrade to "not
    measured" on anything older.

    None rather than 0 is the load-bearing part: a summary line reporting a peak
    of 0.00 GiB reads as a night with enormous headroom, which is the exact
    conclusion #305 exists to prevent someone drawing without evidence.
    """
    root, proc = _tree(tmp_path, memory_high=str(40 * 2**30), memory_max=str(48 * 2**30))

    assert cgroup_memory.read_cgroup_memory(root, proc) is None
    assert cgroup_memory.describe_cgroup_memory(root, proc) is None


def test_cgroup_v1_is_declined_rather_than_half_parsed(tmp_path):
    """v1 has no `memory.peak`, so a v1 host has nothing to report and saying so
    at the hierarchy level is more legible than a path lookup that fails one
    level deeper."""
    root = tmp_path / "cgroup"
    root.mkdir()
    proc = tmp_path / "proc_self_cgroup"
    proc.write_text("11:memory:/user.slice\n4:cpu,cpuacct:/user.slice\n")

    assert cgroup_memory.read_cgroup_memory(str(root), str(proc)) is None


@pytest.mark.parametrize(
    "peak_contents",
    ["", "not-a-number", "max"],
    ids=["empty", "garbage", "the-max-sentinel"],
)
def test_an_unparseable_peak_is_not_measured(tmp_path, peak_contents):
    """Whatever a future kernel or a container runtime writes there, the tail
    must not raise. `max` is included deliberately: it is a legal value for the
    *limit* files, and reusing the same reader means it has to mean "unknown"
    here rather than an enormous int."""
    root, proc = _tree(tmp_path, memory_peak=peak_contents, memory_high=str(40 * 2**30))

    assert cgroup_memory.read_cgroup_memory(root, proc) is None


def test_nothing_raises_when_the_paths_do_not_exist_at_all(tmp_path):
    """This runs in the batch tail, beside the catalog backup and the publish.
    Issue #167's rule is that nothing in there may cost the night its publish,
    and an accounting nicety least of all — so a wholly absent /sys/fs/cgroup
    (macOS, a hardened container) is a None, never an OSError."""
    missing = str(tmp_path / "nope")

    assert cgroup_memory.read_cgroup_memory(missing, missing) is None
    assert cgroup_memory.describe_cgroup_memory(missing, missing) is None


def test_the_real_host_is_read_without_raising():
    """Belt and braces against the fixtures above being wrong about the world:
    call it with the module's real defaults. The assertion is deliberately weak
    — CI runs on Linux and macOS, in and out of containers, so the ANSWER is not
    portable — but "returns a string or None, never an exception" is."""
    result = cgroup_memory.describe_cgroup_memory()

    assert result is None or (isinstance(result, str) and result.startswith("cgroup peak "))


def test_the_module_defaults_point_at_the_real_cgroup_paths():
    """Pin the pass-through, not just the shape: every test above supplies its
    own fixture paths, so a typo in the production defaults would be invisible
    to all of them."""
    assert cgroup_memory.CGROUP_ROOT == "/sys/fs/cgroup"
    assert cgroup_memory.PROC_SELF_CGROUP == "/proc/self/cgroup"
    assert os.path.isabs(cgroup_memory.CGROUP_ROOT)
