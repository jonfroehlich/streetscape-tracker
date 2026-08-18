"""Tests for the shared progress helper (streetscape_metadata_tracker.progress).

Two things are being protected here, and they are not the same thing:

1. A progress bar must never be a crash surface in a headless or piped run. The
   subtle part is that tqdm's own ``disable=None`` does NOT achieve this — it
   inspects stderr and then flushes both streams — so the live-stderr/dead-stdout
   case still raises. That regression is cheap to reintroduce ("why not just use
   disable=None?"), so it gets its own test.
2. Turning bars off must not make long headless collections silent, because this
   project diagnoses hangs from collector output.
"""

import io
import logging
import pathlib
import re

import pytest

from streetscape_metadata_tracker.progress import Progress, bars_enabled, progress


class FakeStream(io.StringIO):
    """A stream whose ``isatty`` we control."""

    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class ExplodingStream(io.StringIO):
    """A stream that cannot answer ``isatty`` — e.g. an already-closed one."""

    def isatty(self) -> bool:
        raise ValueError("I/O operation on closed file")


# ── the both-streams guard ────────────────────────────────────────────────────


def test_a_bar_is_drawn_only_when_both_streams_are_terminals(monkeypatch):
    monkeypatch.setattr("sys.stdout", FakeStream(tty=True))
    monkeypatch.setattr("sys.stderr", FakeStream(tty=True))
    assert bars_enabled() is True


def test_a_dead_stdout_disables_the_bar_even_though_stderr_is_live(monkeypatch):
    """The finding tqdm's own disable=None gets wrong, and the reason this
    helper exists.

    tqdm decides using ``file`` alone, which defaults to sys.stderr — but when
    the bar is live its status_printer flushes the raw sys.stdout too (and
    DisableOnWriteError.__eq__ proxies to the wrapped stream, so the membership
    test that guards that flush is True). So `run-due | head` — a terminal
    stderr, a pipe whose reader has gone away on stdout, i.e. the 2026-08-17
    incident command WITHOUT `2>&1` — left the bar enabled and raised
    BrokenPipeError out of the stdout flush.
    """
    monkeypatch.setattr("sys.stdout", FakeStream(tty=False))
    monkeypatch.setattr("sys.stderr", FakeStream(tty=True))
    assert bars_enabled() is False, (
        "a bar that inspects only stderr is the bug this module was written to fix"
    )
    assert progress(total=3, desc="x").enabled is False


def test_a_dead_stderr_disables_the_bar_too(monkeypatch):
    monkeypatch.setattr("sys.stdout", FakeStream(tty=True))
    monkeypatch.setattr("sys.stderr", FakeStream(tty=False))
    assert bars_enabled() is False


def test_a_stream_that_cannot_say_counts_as_not_a_terminal(monkeypatch):
    """Closed streams, pytest capture and systemd's journal all turn up here.
    Failing closed (bar off) is the safe direction."""
    monkeypatch.setattr("sys.stdout", ExplodingStream())
    monkeypatch.setattr("sys.stderr", FakeStream(tty=True))
    assert bars_enabled() is False


def test_streams_are_read_at_call_time_not_bound_at_import(monkeypatch):
    """A caller that redirects its own output must be seen; binding sys.stdout
    at import would make the guard answer for the wrong streams."""
    monkeypatch.setattr("sys.stdout", FakeStream(tty=True))
    monkeypatch.setattr("sys.stderr", FakeStream(tty=True))
    assert bars_enabled() is True
    monkeypatch.setattr("sys.stdout", FakeStream(tty=False))
    assert bars_enabled() is False


# ── the liveness tick ─────────────────────────────────────────────────────────


def _ticking(logger, monkeypatch, clock, **kw) -> Progress:
    """A Progress with bars off (the headless case) and a fake monotonic clock."""
    monkeypatch.setattr("sys.stdout", FakeStream(tty=False))
    monkeypatch.setattr("sys.stderr", FakeStream(tty=False))
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    return progress(logger=logger, **kw)


def test_a_headless_run_logs_progress_instead_of_drawing_it(monkeypatch, caplog):
    """Scheduler children write to a log file, never a TTY, so their bars are
    always off. Without this the per-attempt log of a 247k-request GSV walk is
    blank between its first and last line, and issue #157's "printed nothing
    after Decoded …" diagnosis becomes impossible to make."""
    logger = logging.getLogger("test.progress.tick")
    clock = [0.0]
    bar = _ticking(
        logger, monkeypatch, clock, total=100, desc="Walking", unit="point", tick_seconds=60.0
    )
    assert bar.enabled is False

    with caplog.at_level(logging.INFO, logger="test.progress.tick"):
        for _ in range(10):
            bar.update(1)
        assert caplog.messages == [], "no tick before the interval elapses"

        clock[0] = 61.0
        bar.update(1)
        assert len(caplog.messages) == 1
        assert "Walking" in caplog.messages[0]
        assert "11/100" in caplog.messages[0]
        assert "point" in caplog.messages[0]

        # Still inside the NEW interval: no second line.
        bar.update(1)
        assert len(caplog.messages) == 1

        clock[0] = 130.0
        bar.update(1)
        assert len(caplog.messages) == 2


def test_close_forces_a_final_line_so_a_log_shows_where_work_ended(monkeypatch, caplog):
    logger = logging.getLogger("test.progress.final")
    clock = [0.0]
    bar = _ticking(logger, monkeypatch, clock, total=5, desc="Fetching")

    with caplog.at_level(logging.INFO, logger="test.progress.final"):
        bar.update(5)
        assert caplog.messages == []
        bar.close()
        assert len(caplog.messages) == 1
        assert "5/5" in caplog.messages[0]

        bar.close()  # idempotent
        assert len(caplog.messages) == 1


def test_without_a_logger_a_disabled_bar_is_silent(monkeypatch, caplog):
    """Fast work (grid generation, the aggregate) opts out: ticks there would be
    noise, not liveness."""
    clock = [0.0]
    bar = _ticking(None, monkeypatch, clock, total=10, desc="Quick")
    with caplog.at_level(logging.INFO):
        bar.update(10)
        clock[0] = 9999.0
        bar.update(0)
        bar.close()
    assert caplog.messages == []


def test_a_totalless_bar_still_reports_a_count(monkeypatch, caplog):
    logger = logging.getLogger("test.progress.nototal")
    clock = [0.0]
    bar = _ticking(logger, monkeypatch, clock, desc="Streaming", unit="row")
    with caplog.at_level(logging.INFO, logger="test.progress.nototal"):
        bar.update(7)
        bar.close()
    assert len(caplog.messages) == 1
    assert "7 row" in caplog.messages[0]
    assert "%" not in caplog.messages[0], "no percentage without a denominator"


# ── the shapes the call sites use ─────────────────────────────────────────────


def test_iteration_yields_every_item_and_counts_it(monkeypatch):
    """tqdm's own __iter__ short-circuits past update() when the bar is
    disabled, which is why iteration is driven here — otherwise a tick could
    only ever fire in the TTY case that does not need it."""
    monkeypatch.setattr("sys.stdout", FakeStream(tty=False))
    monkeypatch.setattr("sys.stderr", FakeStream(tty=False))
    items = list(range(5))
    bar = progress(items, desc="Iterating")
    assert list(bar) == items
    assert bar.n == 5
    assert bar.total == 5, "a sized iterable supplies its own total"


def test_iteration_works_on_an_iterator_with_no_length(monkeypatch):
    monkeypatch.setattr("sys.stdout", FakeStream(tty=False))
    monkeypatch.setattr("sys.stderr", FakeStream(tty=False))
    bar = progress(iter("abc"), desc="Streaming")
    assert bar.total is None
    assert list(bar) == ["a", "b", "c"]


def test_context_manager_form_closes(monkeypatch, caplog):
    logger = logging.getLogger("test.progress.ctx")
    clock = [0.0]
    monkeypatch.setattr("sys.stdout", FakeStream(tty=False))
    monkeypatch.setattr("sys.stderr", FakeStream(tty=False))
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    with caplog.at_level(logging.INFO, logger="test.progress.ctx"):
        with progress(total=4, desc="Gridding", logger=logger) as bar:
            bar.update(4)
        assert len(caplog.messages) == 1, "__exit__ must close, which forces the final line"


def test_iterating_without_an_iterable_is_an_error(monkeypatch):
    monkeypatch.setattr("sys.stdout", FakeStream(tty=False))
    monkeypatch.setattr("sys.stderr", FakeStream(tty=False))
    with pytest.raises(TypeError):
        list(progress(total=3, desc="nope"))


def test_a_live_bar_still_advances_the_real_tqdm(monkeypatch):
    """The TTY path is the one humans see; make sure wrapping did not break it."""
    monkeypatch.setattr("sys.stdout", FakeStream(tty=True))
    monkeypatch.setattr("sys.stderr", FakeStream(tty=True))
    bar = progress(total=3, desc="Live")
    assert bar.enabled is True
    bar.update(2)
    assert bar.n == 2
    assert bar._bar.n == 2
    bar.close()


# ── the rule itself ───────────────────────────────────────────────────────────


def test_no_module_calls_tqdm_directly():
    """The rule has to be enforced, not documented.

    Seven call sites were once fixed by hand with `disable=None`; a rule that
    lives only in prose means the eighth reintroduces the 2026-08-17 failure.
    Same posture as test_host_lock's assertion that scheduler.py never imports
    host_lock — cheap source inspection beating a comment nobody reads.
    """
    import streetscape_metadata_tracker.progress as prog

    root = pathlib.Path(prog.__file__).parent.parent
    allowed = {pathlib.Path(prog.__file__).resolve()}
    call = re.compile(r"\btqdm\s*\(")

    offenders = []
    for package in ("streetscape_metadata_tracker", "streetscape_street_analyzer", "scripts"):
        for path in sorted((root / package).rglob("*.py")):
            if path.resolve() in allowed:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue  # a comment may name tqdm to explain why not to use it
                if call.search(line):
                    offenders.append(f"{path.relative_to(root)}:{lineno}")

    assert offenders == [], (
        "these construct tqdm directly instead of calling progress(): "
        + ", ".join(offenders)
        + ". progress() carries the both-streams guard and the headless liveness "
        "tick; a bare tqdm has neither."
    )
