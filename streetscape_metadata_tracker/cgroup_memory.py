"""This process's cgroup memory peak, for the nightly summary line (issue #305).

The systemd unit caps the batch with ``MemoryHigh``/``MemoryMax`` (see
``deploy/systemd/streetscape-tracker.service``), and ``MemoryHigh`` is a
*throttle*: crossing it does not fail, it costs hours of silent reclaim that end
in the scheduler's per-city timeout SIGKILLing a child that printed nothing
(issue #157, measured 2026-08-18). So "how close did we get to the cap?" is the
question that separates a slow night from a resource limit — and until #305
nothing recorded it. The reading existed (``systemctl --user show
streetscape-tracker.service -p MemoryPeak``), but only for an operator logged in
at the right moment, and the value resets when the next start creates a new
cgroup. A night's peak was therefore unobservable by morning.

**Two numbers, not one, because the percentage is only a proxy.**
``memory.peak`` is the high-water mark of ``memory.current``, which charges page
cache, kernel memory and socket buffers alongside anonymous memory — so it is
not the same quantity as the ~15.3 GiB working-set figure the unit file
extrapolates from a per-city slope, and a high percentage does not by itself
mean the night was throttled. ``memory.events``'s ``high`` counter is the direct
evidence: it counts the times processes in this cgroup were pushed into direct
reclaim *because* the high boundary was exceeded. Zero throttle events at 94% of
``MemoryHigh`` says the brake never engaged; a non-zero count at 47% says it did,
and no percentage would have shown it. Both are read here, and the counter shares
the peak's lifetime semantics, so the two are always about the same window.

Three things this deliberately does NOT do:

* **It never raises.** A missing file, a cgroup v1 host, a kernel without
  ``memory.peak`` (added in 5.19), a container that hides ``/sys/fs/cgroup``,
  an unparseable or non-ASCII value, or a cap of ``0`` (a legal cgroup v2
  setting, and what ``systemctl set-property MemoryHigh=0`` writes) all return
  ``None`` or drop the percentage. This runs in the tail, beside the catalog
  backup and the publish, and ``_finish_batch`` is NOT wrapped at its call site
  — an exception here skips the backup, the publish and the alert, which is
  exactly issue #167's failure. So every read catches ``ValueError`` as well as
  ``OSError`` (``UnicodeDecodeError`` is a ``ValueError``), and every division
  checks its denominator is truthy rather than merely non-``None``.
* **It reads the cgroup, not systemd.** ``systemctl show`` would need the
  service name, would only work under the unit, and would spawn a child inside
  the very cgroup being measured. ``/proc/self/cgroup`` needs none of that and
  reports something meaningful for a manual run too.
* **It does not distinguish the scheduler from its children.** It cannot:
  ``subprocess`` children stay in the parent's cgroup, which is exactly what
  makes this the right number — the cap applies to the whole unit, so the peak
  that matters is the whole unit's. Under ``max_concurrent_channels > 1`` that
  is several collection children at once, which is the case #305 exists to size.

Peak semantics follow the cgroup's lifetime, and that differs by caller in a way
worth knowing before comparing two numbers: under the systemd unit the cgroup is
created per start, so the reading is *this night's* peak. Run by hand from a
login shell the cgroup is the session scope, so the reading is the peak of
everything that shell has done since login — still useful, not comparable to a
nightly figure.
"""

import os
from dataclasses import dataclass

# Module-level so tests can point the reader at a fixture tree; not read from
# the environment, because an operator-settable path here would silently make
# the number describe a different cgroup than the one the caps apply to.
CGROUP_ROOT = "/sys/fs/cgroup"
PROC_SELF_CGROUP = "/proc/self/cgroup"

_GIB = float(2**30)


@dataclass(frozen=True)
class CgroupMemory:
    """One reading of the cgroup this process belongs to.

    ``high_bytes``/``max_bytes`` are ``None`` when the corresponding cap is
    unset — cgroup v2 spells that ``max``, and "no cap" and "a cap we could not
    read" are deliberately the same value here, because the only thing either
    can support is declining to quote a percentage.

    ``high_events`` is ``memory.events``'s ``high`` counter — how many times
    this cgroup was actually throttled at the soft brake — or ``None`` when the
    file is absent or unparseable. ``None`` and ``0`` are very different
    answers here and callers must keep them apart: ``0`` is the reassuring
    measurement the percentage cannot give, while ``None`` is no measurement.
    """

    peak_bytes: int
    high_bytes: int | None
    max_bytes: int | None
    high_events: int | None = None


def _read_limit(path: str) -> int | None:
    """One cgroup byte-valued file, or None for ``max`` / unreadable.

    ``ValueError`` is caught alongside ``OSError`` because the decode happens
    inside the read: a non-ASCII byte raises ``UnicodeDecodeError``, which is a
    ``ValueError`` and would otherwise escape the whole module's never-raises
    contract on the way out of ``fh.read()``.
    """
    try:
        with open(path, encoding="ascii") as fh:
            raw = fh.read().strip()
    except (OSError, ValueError):
        return None
    if raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _read_high_events(path: str) -> int | None:
    """``memory.events``'s ``high`` counter, or None.

    The file is ``key value`` lines (``low``/``high``/``max``/``oom``/
    ``oom_kill``), so it needs its own parse rather than ``_read_limit``. Only
    ``high`` is read: it is the one counter that answers "did the soft brake
    engage?", which is the question the peak percentage can only approximate.
    """
    try:
        with open(path, encoding="ascii") as fh:
            for line in fh:
                key, _, value = line.strip().partition(" ")
                if key == "high":
                    return int(value)
    except (OSError, ValueError):
        return None
    return None


def _own_cgroup_path(cgroup_root: str, proc_self_cgroup: str) -> str | None:
    """Filesystem path of this process's cgroup v2 node, or None.

    The ``0::`` prefix is the unified (v2) hierarchy and is the only line this
    accepts. That is not laziness about v1: v1 has no ``memory.peak`` at all, so
    parsing its ``N:memory:/path`` lines would only produce a path whose files
    do not exist, and reporting "unavailable" for the right reason is worth more
    than a lookup that fails one level deeper.
    """
    try:
        with open(proc_self_cgroup, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("0::"):
                    relative = line.strip().split("::", 1)[1]
                    # An empty path is the root cgroup, which is legal.
                    return os.path.join(cgroup_root, relative.lstrip("/"))
    except (OSError, ValueError):
        return None
    return None


def read_cgroup_memory(
    cgroup_root: str = CGROUP_ROOT,
    proc_self_cgroup: str = PROC_SELF_CGROUP,
) -> CgroupMemory | None:
    """This process's cgroup memory peak, caps and throttle count, or None.

    ``None`` means "not measured", never "zero" — every caller has to keep those
    apart, since a summary line quoting a peak of 0 GiB on a host without
    ``memory.peak`` would read as a healthy night with enormous headroom.
    """
    base = _own_cgroup_path(cgroup_root, proc_self_cgroup)
    if base is None:
        return None
    peak = _read_limit(os.path.join(base, "memory.peak"))
    if peak is None:
        return None
    return CgroupMemory(
        peak_bytes=peak,
        high_bytes=_read_limit(os.path.join(base, "memory.high")),
        max_bytes=_read_limit(os.path.join(base, "memory.max")),
        high_events=_read_high_events(os.path.join(base, "memory.events")),
    )


def format_cgroup_memory(reading: CgroupMemory) -> str:
    """One human line, e.g.::

        cgroup peak 18.88 GiB of 40 GiB MemoryHigh (47%), throttled 0 times

    Quoted against ``MemoryHigh`` in preference to ``MemoryMax`` because that is
    the cap whose breach is invisible: reaching ``MemoryMax`` produces an OOM
    kill an operator can read in a log, while reaching ``MemoryHigh`` produces a
    night that is merely slow. The percentage is why the line is worth reading —
    the absolute GiB alone cannot say whether a raise is due — and the throttle
    count is why the percentage is not the last word.

    The count is appended only in the ``MemoryHigh`` branch: ``memory.events``'s
    ``high`` counter increments on the *high* boundary, so with no soft brake
    configured it is structurally 0 and reporting it would be noise dressed as a
    measurement.

    Both caps are tested for truthiness, not just for ``None``. ``0`` is a legal
    value for ``memory.high`` and ``memory.max`` — it is what
    ``systemctl --user set-property ... MemoryHigh=0`` writes, on the same
    surface ``deploy/README.md`` documents for live emergencies — and dividing
    by it is how this becomes the exception that costs the night its publish.
    """
    peak = f"cgroup peak {reading.peak_bytes / _GIB:.2f} GiB"
    if reading.high_bytes:
        cap, label = reading.high_bytes, "MemoryHigh"
        throttled = (
            f", throttled {reading.high_events} time{'' if reading.high_events == 1 else 's'}"
            if reading.high_events is not None
            else ""
        )
    elif reading.max_bytes:
        cap, label, throttled = reading.max_bytes, "MemoryMax", ""
    else:
        return f"{peak} (no cgroup memory cap)"
    pct = 100.0 * reading.peak_bytes / cap
    return f"{peak} of {cap / _GIB:.0f} GiB {label} ({pct:.0f}%){throttled}"


def describe_cgroup_memory(
    cgroup_root: str = CGROUP_ROOT,
    proc_self_cgroup: str = PROC_SELF_CGROUP,
) -> str | None:
    """The formatted line, or None when the reading is unavailable.

    The one-call form the scheduler tail uses. Silence is the correct output on
    a host that cannot answer — an operator grepping for ``cgroup peak`` learns
    nothing from a line saying the number is unknown, and the nightly summary is
    already dense.
    """
    reading = read_cgroup_memory(cgroup_root, proc_self_cgroup)
    return format_cgroup_memory(reading) if reading else None
