"""Progress reporting that survives a headless run and a dead output pipe.

Every progress bar in this project goes through :func:`progress`. That is a hard
rule (pinned by a source-inspection test in ``tests/test_progress.py``), because
a progress bar is otherwise a crash surface in exactly the runs that matter
most, and the correct incantation is not the obvious one.

**Why not just ``tqdm(..., disable=None)``.** ``disable=None`` means "off unless
the stream is a TTY", which reads like the whole fix and is not. tqdm decides
using ``file`` alone — ``std.py``'s ``if disable is None and hasattr(file,
"isatty") and not file.isatty()`` — and ``file`` defaults to ``sys.stderr``. But
when the bar IS live, ``status_printer`` flushes **both** raw streams::

    if fp in (sys.stderr, sys.stdout):
        getattr(sys.stderr, 'flush', lambda: None)()
        getattr(sys.stdout, 'flush', lambda: None)()

and ``DisableOnWriteError.__eq__`` proxies to the wrapped stream, so that
membership test is True even though ``fp`` is a wrapper. So a live stderr and a
dead stdout — ``run-due | head``, i.e. the 2026-08-17 incident command *without*
``2>&1`` — leaves the bar enabled and raises ``BrokenPipeError`` out of the
stdout flush. There is reliably buffered data there to fail on, because
``scheduler.setup_logging`` installs a ``StreamHandler(sys.stdout)``.

Hence :func:`bars_enabled`: a bar is live only when **both** streams are
terminals. Do not "simplify" this back to ``disable=None``.

**Why a logger tick.** Scheduler children run with stdout/stderr redirected to
``logs/collect_{city_id}_{channel}_{date}.log``, which is never a TTY, so the
guard above means their bars are always off. Silence is not free: this project's
hang diagnoses are built on collector output (issue #157's was "hit the
180-minute timeout every night having printed nothing after ``Decoded …``"), and
with no progress at all a SIGKILLed child leaves "hung" and "merely slow"
indistinguishable. So when the bar is off and a ``logger`` is supplied, progress
is emitted as periodic log lines instead — one line per ``tick_seconds``, plus a
line at close. That keeps the liveness signal while keeping ``\\r`` refresh
frames out of the per-attempt logs an operator has to read.

Iteration is driven here rather than handed to tqdm on purpose: tqdm's
``__iter__`` short-circuits to a bare ``for obj in iterable`` when the bar is
disabled and never calls ``update``, so a tick built on top of it would fire
only in the TTY case that does not need it.
"""

import logging
import sys
import time
from collections.abc import Iterable, Iterator
from typing import Any

from tqdm import tqdm

# One line a minute is enough to tell "working" from "wedged" in a log an
# operator reads after the fact, and small enough that a 3-hour city adds ~180
# lines rather than a bar's worth of refresh frames.
DEFAULT_TICK_SECONDS = 60.0


def _is_tty(stream: Any) -> bool:
    """True if ``stream`` is a terminal, tolerating streams that can't say.

    pytest's capture objects, systemd's journal streams and a closed stdout all
    turn up here; anything that raises or lacks ``isatty`` is treated as not a
    terminal, which is the safe direction (bar off).
    """
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def bars_enabled() -> bool:
    """True only when BOTH stdout and stderr are terminals.

    Read the module docstring before changing this: checking stderr alone (which
    is what ``tqdm(disable=None)`` does) leaves the live-stderr/dead-stdout case
    crashing, and that is half of the failure this exists to prevent.

    ``sys.stdout``/``sys.stderr`` are looked up at call time, not bound at
    import, so a caller (or a test) that redirects them is seen.
    """
    return _is_tty(sys.stdout) and _is_tty(sys.stderr)


class Progress:
    """A progress bar when attached to a terminal, periodic log lines otherwise.

    Supports the three shapes the call sites use: iteration
    (``for x in progress(items, ...)``), manual ``update()``/``close()``, and
    use as a context manager. Construct via :func:`progress`.
    """

    def __init__(
        self,
        iterable: Iterable | None = None,
        *,
        total: int | None = None,
        initial: int = 0,
        desc: str | None = None,
        unit: str = "it",
        logger: logging.Logger | None = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        **tqdm_kwargs: Any,
    ) -> None:
        self.iterable = iterable
        if total is None and iterable is not None:
            try:
                total = len(iterable)  # type: ignore[arg-type]
            except (TypeError, AttributeError):
                total = None
        self.total = total
        self.n = initial

        self._desc = desc or "progress"
        self._unit = unit
        self._logger = logger
        self._tick_seconds = tick_seconds
        self._started = time.monotonic()
        self._last_tick = self._started
        self._closed = False

        # The iterable is deliberately NOT handed to tqdm — see the module
        # docstring. We drive iteration so update() runs in both modes.
        self._bar = (
            tqdm(total=total, initial=initial, desc=desc, unit=unit, **tqdm_kwargs)
            if bars_enabled()
            else None
        )

    @property
    def enabled(self) -> bool:
        """Whether a real bar is being drawn (both streams are terminals)."""
        return self._bar is not None

    def update(self, n: int = 1) -> None:
        self.n += n
        if self._bar is not None:
            self._bar.update(n)
        else:
            self._maybe_tick()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._bar is not None:
            self._bar.close()
        else:
            # Force a final line so a log shows where the work ended, not just
            # where it last ticked.
            self._maybe_tick(force=True)

    def __iter__(self) -> Iterator:
        if self.iterable is None:
            raise TypeError("progress() was constructed without an iterable")
        try:
            for item in self.iterable:
                yield item
                self.update(1)
        finally:
            self.close()

    def __enter__(self) -> "Progress":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self.close()
        return False

    def _maybe_tick(self, force: bool = False) -> None:
        if self._logger is None:
            return
        now = time.monotonic()
        if not force and now - self._last_tick < self._tick_seconds:
            return
        self._last_tick = now
        self._logger.info(self._status(now))

    def _status(self, now: float) -> str:
        elapsed_min = (now - self._started) / 60.0
        if self.total:
            pct = 100.0 * self.n / self.total
            return (
                f"{self._desc}: {self.n:,}/{self.total:,} {self._unit} "
                f"({pct:.0f}%) after {elapsed_min:.1f} min"
            )
        return f"{self._desc}: {self.n:,} {self._unit} after {elapsed_min:.1f} min"


def progress(
    iterable: Iterable | None = None,
    *,
    total: int | None = None,
    initial: int = 0,
    desc: str | None = None,
    unit: str = "it",
    logger: logging.Logger | None = None,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
    **tqdm_kwargs: Any,
) -> Progress:
    """Make a :class:`Progress`. Use this instead of ``tqdm`` anywhere in this repo.

    Args:
        iterable: What to iterate, if using the ``for x in progress(...)`` form.
        total: Item count; inferred from ``iterable`` when it has a length.
        initial: Items already done (e.g. a resumed ``.downloading`` run).
        desc: Label, shown on the bar and in the log lines.
        unit: Noun for the items, e.g. ``"city"``.
        logger: Where to send periodic progress when no bar is drawn. Pass this
            for long-running work the scheduler runs headless; omit it for fast
            work, where ticks would be noise.
        tick_seconds: Minimum gap between those log lines.
        **tqdm_kwargs: Forwarded to ``tqdm`` when a bar is actually drawn.

    Example:
        >>> for city in progress(cities, desc="Aggregating cities", unit="city"):
        ...     summarize(city)
    """
    return Progress(
        iterable,
        total=total,
        initial=initial,
        desc=desc,
        unit=unit,
        logger=logger,
        tick_seconds=tick_seconds,
        **tqdm_kwargs,
    )
